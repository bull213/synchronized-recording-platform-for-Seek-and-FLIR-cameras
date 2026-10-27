"""Per-camera recovery supervised independently of the Qt event loop."""
from __future__ import annotations

from copy import deepcopy
import shutil
import threading
import time

from camera_reliability import MIN_FREE_BYTES, WARN_FREE_BYTES, PowerInhibitor, log
from camera_runtime import reconnect_flir_camera, _release_flir_camera


class CameraHealth:
    STALE_NS = 5_000_000_000
    RETRY_SECONDS = (1, 2, 5, 10, 30)

    def _init_health(self):
        self.health = {}
        self._monitor_stop = threading.Event()
        self._monitor = None
        self._recovery_cancel = threading.Event()
        self._recovery_threads = {}
        self._recovery_pending = set()
        self._enumeration_lock = threading.Lock()
        self._next_checkpoint_ns = 0
        self._next_disk_check_ns = 0
        self._low_space_warning = False
        self._writer_unhealthy = False
        self._shutter_failures = 0
        self.free_disk_bytes = None
        self._last_shutter_tick_ns = 0

    def _recovery_busy(self):
        return bool(self._recovery_pending) or any(worker.is_alive() for worker in self._recovery_threads.values())

    def _health(self, stream):
        return self.health.setdefault(stream, dict(state='waiting', generation=0,
            last_received_ns=None, last_written_ns=None, attempts=0, recovery_count=0,
            retry_at_ns=0, waiting_since_ns=None, force_recovery=False, gap_start_ns=None,
            last_error=None, last_error_ns=0, dropped_frames=0, writing_stalled=False))

    def _selected_streams(self):
        mode = self.options['recording_mode']
        if mode == 'microphone_mp3':
            return []
        names = [f"flir_cam{cam['index']}" for cam in self.flir_cams]
        return (['seek'] if mode == 'combined' else []) + names

    def _ensure_monitor(self):
        if self._monitor is None:
            self._monitor = threading.Thread(target=self._monitor_loop, name='Recording watchdog', daemon=True)
            self._monitor.start()

    def _stream_seen(self, stream, received_ns, generation=None):
        """Caller holds coordinator lock. Only valid, current frames reset liveness."""
        health = self._health(stream)
        if generation is not None and generation != health['generation']:
            return False
        previous = health['last_received_ns']
        if previous is not None and received_ns <= previous:
            return False
        if health['gap_start_ns'] is not None and self.is_recording:
            self._record_gap(stream, health['gap_start_ns'], received_ns, 'camera interruption')
            self._event('CAMERA_RECOVERED', stream, dict(generation=health['generation']))
        health.update(state='healthy', last_received_ns=received_ns, force_recovery=False,
                      gap_start_ns=None, attempts=0, retry_at_ns=0, waiting_since_ns=None)
        if stream == 'seek':
            self.seek_ready = True
        self.cameras_ready = self.seek_ready and bool(self.flir_cams) and all(
            self._health(f"flir_cam{cam['index']}")['state'] == 'healthy' for cam in self.flir_cams)
        if self.is_recording and stream in self._selected_streams():
            counts = self.take['received_frame_counts']
            counts[stream] = counts.get(stream, 0) + 1
        return True

    def camera_fault(self, stream, message, sdk_code=None, generation=None, disconnected=False):
        """A device fault is evidence for recovery, never a command to stop a take."""
        now = time.perf_counter_ns()
        with self.lock:
            health = self._health(stream)
            if generation is not None and generation != health['generation']:
                return
            notify = message != health['last_error'] or now - health['last_error_ns'] >= 5_000_000_000
            health['last_error'] = message
            if notify:
                health['last_error_ns'] = now
                log.warning('%s generation=%s sdk_code=%s: %s', stream, health['generation'], sdk_code, message)
                self.core.ui_events.put(('writer_warning', message))
                if self.is_recording and stream in self._selected_streams():
                    self._event('CAMERA_WARNING', stream, dict(message=message, sdk_code=sdk_code,
                                                              generation=health['generation']))
            if disconnected:
                # Invalidate frames already in flight from the removed device.
                health['generation'] += 1
                health['force_recovery'] = True
                health['gap_start_ns'] = health['last_received_ns'] or now
                health['state'] = 'disconnected'
                if stream == 'seek':
                    self.seek_ready = self.cameras_ready = False
                    self.recent_seek.clear()
                    self.shutter_gate.cancel()

    def _record_gap(self, stream, start_ns, end_ns, reason, count=None):
        if not self.take:
            return
        start_ns = min(end_ns, max(start_ns, self.take['shared_start_monotonic_ns'] or self.take['request_monotonic_ns']))
        gap = dict(start_monotonic_ns=start_ns, end_monotonic_ns=end_ns,
                   reason=reason, stream=stream, dropped_frames=count)
        self.take['has_gaps'] = True
        self.take['gap_count'] += 1
        self.take['gaps'].append(gap)
        del self.take['gaps'][:-512]
        self._event('GAP', stream, gap, end_ns)

    def dropped_frame(self, stream, host_ns, take_id, reason='recording buffer full'):
        with self.lock:
            if not self.take or self.take_number != take_id:
                return
            counts = self.take['dropped_frame_counts']
            counts[stream] = counts.get(stream, 0) + 1
            self._health(stream)['dropped_frames'] += 1
            self.take['has_gaps'] = True
            # Coalesce bursts; never enqueue one diagnostic per dropped video frame.
            gap = self._pending_drops.setdefault(stream, dict(start_ns=host_ns, end_ns=host_ns, count=0, reason=reason))
            gap['end_ns'] = host_ns
            gap['count'] += 1

    def _flush_drops(self):
        for stream, gap in self._pending_drops.items():
            self._record_gap(stream, gap['start_ns'], gap['end_ns'], gap['reason'], gap['count'])
        self._pending_drops.clear()

    def frame_written(self, stream, host_ns, take_id):
        with self.lock:
            if not self.take or take_id != self.take_number:
                return
            counts = self.take['written_frame_counts']
            counts[stream] = counts.get(stream, 0) + 1
            self._health(stream)['last_written_ns'] = time.perf_counter_ns()

    def segment_update(self, segment):
        with self.lock:
            if self.take and segment['take_id'] == self.take_number:
                self.take['segments'][segment['file']] = dict(segment)
                self._event('VIDEO_PART', segment['stream'], segment)
                self._save_report()
        log.info('Video part %s: %s', segment['file'], segment['status'])

    def _monitor_loop(self):
        guard = PowerInhibitor()
        power_warning = False
        try:
            while not self._monitor_stop.is_set():
                try:
                    guard.set_active(self.state in ('starting', 'recording', 'finalizing'))
                except Exception as exc:
                    if not power_warning:
                        self.core.ui_events.put(('writer_warning', str(exc)))
                        log.exception('Recording power guard failed')
                        power_warning = True
                try:
                    self._monitor_once()
                except Exception as exc:
                    log.exception('Recording watchdog failed')
                    self.fault(f'Recording watchdog: {exc}')
                self._monitor_stop.wait(.5)
        finally:
            try:
                guard.set_active(False)
            except Exception:
                log.exception('Releasing recording power guard')

    def _monitor_once(self, now_ns=None):
        now = time.perf_counter_ns() if now_ns is None else now_ns
        jobs = []
        with self.lock:
            if not self.is_recording or self.closing:
                return
            self._flush_drops()
            if self.options['sample_mode'] == 'after_shutter' and 'seek' in self._selected_streams():
                # The shutter worker may fail while ordinary previews stay live.
                if now - self._last_shutter_tick_ns > self.STALE_NS or self._shutter_failures >= 2:
                    self._health('seek')['force_recovery'] = True
            for stream in self._selected_streams():
                health = self._health(stream)
                worker = self._recovery_threads.get(stream)
                if stream in self._recovery_pending or (worker and worker.is_alive()):
                    continue
                last = health['waiting_since_ns'] or health['last_received_ns'] or self.take['request_monotonic_ns']
                stale = now - last >= self.STALE_NS
                if (stale or health['force_recovery']) and now >= health['retry_at_ns']:
                    health['gap_start_ns'] = health['gap_start_ns'] or health['last_received_ns'] or last
                    health['state'] = 'recovering'
                    health['generation'] += 1
                    health['attempts'] += 1
                    health['recovery_count'] += 1
                    if stream == 'seek':
                        self.shutter_gate.cancel()
                        self.recent_seek.clear()
                        self.seek_ready = self.cameras_ready = False
                    self.take['has_gaps'] = True
                    self.take['recovery_counts'][stream] = self.take['recovery_counts'].get(stream, 0) + 1
                    self._event('RECOVERY_ATTEMPT', stream, dict(generation=health['generation'],
                                                               attempt=health['attempts']))
                    task = threading.Thread(target=self._recover_stream,
                        args=(stream, health['generation'], self.take_number, self._recovery_cancel),
                        daemon=True, name=f'Recover {stream}')
                    self._recovery_threads[stream] = task
                    self._recovery_pending.add(stream)
                    jobs.append(task)
            if self.prepared and self.options['recording_mode'] != 'microphone_mp3':
                if not self.writer or not self.writer.is_alive():
                    self._writer_unhealthy = True
                    self.fault('Camera disk writer stopped unexpectedly.')
                buffered = self.core.save_q.snapshot()
                for stream in self._selected_streams():
                    health = self._health(stream)
                    last = health['last_written_ns'] or self.take['request_monotonic_ns']
                    stalled = bool(buffered['stream_bytes'].get(stream)) and now - last > 10_000_000_000
                    if stalled and not health['writing_stalled']:
                        self._event('WRITER_STALLED', stream, dict(queued_bytes=buffered['stream_bytes'][stream]))
                        self.core.ui_events.put(('writer_warning', f'{stream}: disk writing is falling behind.'))
                    health['writing_stalled'] = stalled
            if self.journal and self.journal.error:
                self.fault(self.journal.error)
            if now >= self._next_checkpoint_ns:
                self._next_checkpoint_ns = now + 30_000_000_000
                self._save_report()
            check_disk = (self.prepared and self.options['recording_mode'] != 'microphone_mp3'
                          and now >= self._next_disk_check_ns)
            if check_disk:
                self._next_disk_check_ns = now + 1_000_000_000
                disk_path = self.run_dir
        # SDK calls, disk calls and thread starts must not hold the coordinator lock.
        for task in jobs:
            try:
                task.start()
            except Exception:
                with self.lock:
                    self._recovery_pending.discard(task.name.removeprefix('Recover '))
                raise
        if check_disk:
            free = shutil.disk_usage(disk_path).free
            with self.lock:
                self.free_disk_bytes = free
                if free < MIN_FREE_BYTES:
                    self.fault('Camera recording stopped safely: less than 1 GiB free disk space.')
                elif free < WARN_FREE_BYTES and not self._low_space_warning:
                    self._low_space_warning = True
                    self.core.ui_events.put(('writer_warning', 'Less than 10 GiB free in the recording folder.'))
                    self._event('LOW_DISK_SPACE', 'storage', dict(free_bytes=free))
                elif free >= WARN_FREE_BYTES:
                    self._low_space_warning = False

    def _recover_stream(self, stream, generation, take_id, cancel):
        try:
            if cancel.is_set():
                return
            if stream == 'seek':
                self.core.recover_seek(self, generation, cancel)
                with self.lock:
                    if not cancel.is_set() and self.is_recording and self.take_number == take_id:
                        self._shutter_failures = 0
                        self.next_shutter_ns = None
                        self._last_shutter_tick_ns = time.perf_counter_ns()
                        # Recreate a dead shutter worker without duplicating a live one.
                        if not any(w.name == 'Seek shutter' and w.is_alive() for w in self.workers):
                            worker = threading.Thread(target=self.core.shutter_thread, name='Seek shutter', daemon=True)
                            worker.start()
                            self.workers.append(worker)
            else:
                index = int(stream.removeprefix('flir_cam'))
                record = next(cam for cam in self.flir_cams if cam['index'] == index)
                stop = record.get('worker_stop')
                if stop:
                    stop.set()
                worker = record.get('worker')
                if worker:
                    worker.join(2)
                    if worker.is_alive():
                        raise RuntimeError('Acquisition worker has not returned; its camera handle is retained.')
                if cancel.is_set():
                    return
                with self._enumeration_lock:
                    if getattr(self.core, 'demo', False):
                        self.core.recover_flir(record)
                    else:
                        reconnect_flir_camera(self.core.PySpin, self.system, record)
                with self.lock:
                    publish = (not cancel.is_set() and self.is_recording and self.take_number == take_id)
                    if publish:
                        self._start_flir_worker(record, generation)
                if not publish:
                    _release_flir_camera(record)
            with self.lock:
                health = self._health(stream)
                if cancel.is_set() or self.take_number != take_id or not self.is_recording:
                    return
                if health['generation'] == generation and health['state'] != 'healthy':
                    delay = self.RETRY_SECONDS[min(max(health['attempts'] - 1, 0), len(self.RETRY_SECONDS) - 1)]
                    stamp = time.perf_counter_ns()
                    health.update(state='waiting', waiting_since_ns=stamp, force_recovery=False,
                                  retry_at_ns=stamp + max(self.STALE_NS, int(delay * 1e9)))
        except Exception as exc:
            log.warning('Recovery of %s failed: %s', stream, exc)
            with self.lock:
                if cancel.is_set() or self.take_number != take_id:
                    return
                health = self._health(stream)
                delay = self.RETRY_SECONDS[min(max(health['attempts'] - 1, 0), len(self.RETRY_SECONDS) - 1)]
                health.update(state='recovering', force_recovery=True, last_error=str(exc),
                              retry_at_ns=time.perf_counter_ns() + int(delay * 1e9))
                self._event('RECOVERY_FAILED', stream, dict(message=str(exc), retry_seconds=delay))
        finally:
            with self.lock:
                self._recovery_pending.discard(stream)

    def _start_flir_worker(self, record, generation=0):
        stop = threading.Event()
        worker = threading.Thread(target=self.core.flir_thread,
            args=(record['index'], record['cam'], generation, stop),
            daemon=True, name=f"FLIR {record['index']}")
        record.update(worker_stop=stop, worker=worker, generation=generation)
        worker.start()
        # Only current camera workers are retained across repeated reconnects.
        self.workers[:] = [item for item in self.workers if item.is_alive()]
        self.workers.append(worker)

    def health_snapshot(self):
        return dict(stream_health=deepcopy(self.health), buffer=self.core.save_q.snapshot(),
                    free_disk_bytes=self.free_disk_bytes)
