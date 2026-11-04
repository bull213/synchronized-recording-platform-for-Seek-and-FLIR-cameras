"""One recording state machine for cameras, optional audio, and external triggers."""
from __future__ import annotations

import json
import math
import queue
import shutil
import threading
import tempfile
import time
from collections import deque
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from statistics import median

from camera_runtime import SeekShutterFrameGate, release_flir_cameras
from camera_health import CameraHealth
from camera_reliability import (FrameBuffer, RecordingJournal, MIN_FREE_BYTES,
                                frame_stream, frame_time, frame_take, log)
from seek_calibration import get_seek_calibration
from ultramic_studio.core import atomic_json, safe_label, utc_now
from ultramic_studio.export import export_mp3


class RecordingCoordinator(CameraHealth):
    def __init__(self, core):
        self.core = core
        core.coordinator = self
        self.lock = threading.RLock()
        self._init_health()
        core.save_q = FrameBuffer()
        self.journal = None
        self._pending_drops = {}
        self.manager = self.system = self.cam_list = None
        self.flir_cams = []
        self.workers = []
        self.writer = None
        self.run_dir = None
        self._run_mode = None
        self._audio_staging = None
        self.seek_ready = False
        self.take_number = 0
        self.take = None
        self.audio = None
        self.cameras_ready = False
        self.connecting = False
        self.closing = False
        self.state = 'idle'
        self.armed = False
        self.prepared = False
        self.staged = []
        self.recent_seek = deque(maxlen=8)
        self.shutter_gate = SeekShutterFrameGate()
        self.shutter_settings = dict(automatic=True, hz=1.0)
        self.next_shutter_ns = None
        self.options = dict(label='session', output=str(Path.cwd()), sample_mode='custom',
                            sample_hz=1.0, microphone=False, recording_mode='combined',
                            seek_calibration='none', video_segment_seconds=900.0)
        self.connect_done = threading.Event()
        self.connect_done.set()
        self.prepare_done = threading.Event()
        self.prepare_done.set()
        self._prepare_abandoned = threading.Event()
        self.finished = threading.Event()
        self.finished.set()

    @property
    def is_recording(self):
        return self.state in ('starting', 'recording')

    def snapshot(self):
        with self.lock:
            return dict(connected=self.cameras_ready, armed=self.armed,
                        recording=self.state == 'recording', pending=self.state == 'starting',
                        finalizing=self.state == 'finalizing', state=self.state,
                        take_id=self.take_number, session_path=str(self.run_dir) if self.run_dir else None,
                        microphone_enabled=self.options['microphone'],
                        shared_start_monotonic_ns=self.take.get('shared_start_monotonic_ns') if self.take else None,
                        error='; '.join(self.take['errors']) if self.take and self.take['errors'] else None,
                        **self.health_snapshot())

    def configure(self, **options):
        with self.lock:
            if self.state != 'idle':
                raise RuntimeError('Finish the current take before changing recording settings.')
            updated = {**self.options, **options}
            if updated['recording_mode'] not in ('combined', 'flir_mp4', 'microphone_mp3', 'seek_only'):
                raise ValueError('Unknown recording mode.')
            if updated['recording_mode'] != 'combined':
                updated['microphone'] = updated['recording_mode'] == 'microphone_mp3'
            if updated['sample_mode'] not in ('custom', 'all', 'after_shutter'):
                raise ValueError('Unknown Seek sampling mode.')
            if not math.isfinite(updated['sample_hz']) or updated['sample_hz'] <= 0:
                raise ValueError('Seek sampling rate must be positive.')
            if not math.isfinite(updated['video_segment_seconds']) or updated['video_segment_seconds'] <= 0:
                raise ValueError('Video part duration must be positive.')
            get_seek_calibration(updated['seek_calibration'])
            self.options = updated
            self.core.seek_sample_mode = updated['sample_mode']
            self.core.seek_sample_hz = updated['sample_hz']

    def connect(self):
        if self.connecting or self.cameras_ready or self.closing:
            return
        self.connecting = True
        self._ensure_monitor()
        self.connect_done.clear()
        threading.Thread(target=self._connect_worker, name='Camera connection', daemon=True).start()

    def _connect_worker(self):
        core = self.core
        try:
            core.stop_all.clear()
            if not self.seek_ready and self.manager is None:
                try:
                    core.seek_connected.clear()
                    core.first_seek_frame.clear()
                    self.manager = core.start_seek()
                    self.seek_ready = True
                    worker = threading.Thread(target=core.shutter_thread, daemon=True, name='Seek shutter')
                    worker.start()
                    self.workers.append(worker)
                except Exception as exc:
                    core.ui_events.put(('message', f'Seek unavailable: {exc}'))
            if not self.flir_cams:
                try:
                    self.system, self.cam_list, self.flir_cams = core.start_blackfly()
                except Exception as exc:
                    core.ui_events.put(('message', f'FLIR unavailable: {exc}'))
                for cam in self.flir_cams:
                    self._start_flir_worker(cam, self._health(f"flir_cam{cam['index']}")['generation'])
            self.cameras_ready = self.seek_ready and bool(self.flir_cams)
            core.ui_events.put(('connected', self.flir_cams))
        except Exception as exc:
            core.stop_all.set()
            for worker in self.workers:
                if worker.ident is not None:
                    worker.join(timeout=2)
            self._release_cameras()
            core.ui_events.put(('connect_error', str(exc)))
        finally:
            self.connecting = False
            self.connect_done.set()

    def request(self, command, source='manual', **info):
        command = command.upper()
        info.setdefault('request_monotonic_ns', time.perf_counter_ns())
        with self.lock:
            if command == 'STATUS':
                return {'ok': True, **self.snapshot()}
            if source != 'manual':
                if command not in ('START', 'STOP', 'MARK'):
                    return {'ok': False, 'error': 'Only START, STOP, MARK, STATUS are external commands.'}
                if not self.armed:
                    return {'ok': False, 'error': 'Triggers are disarmed.'}
            try:
                if command == 'ARM':
                    self._check_ready()
                    self.armed = True
                elif command == 'DISARM':
                    self.armed = False
                    self._stop(source, info)
                elif command == 'START':
                    if self.state in ('starting', 'recording'):
                        return {'ok': True, 'already_recording': True, **self.snapshot()}
                    self._start(source, info)
                elif command == 'STOP':
                    self._stop(source, info)
                elif command == 'MARK':
                    if not self.is_recording:
                        raise RuntimeError('No recording is active.')
                    self._event('MARK', source, info)
                else:
                    raise ValueError(f'Unknown command {command}.')
                return {'ok': True, **self.snapshot()}
            except Exception as exc:
                return {'ok': False, 'error': str(exc)}

    def _audio_ready(self):
        if self.audio is None:
            return False
        status = self.audio.snapshot()
        return status['connected'] and not status['error'] and status['samples'] > 0

    def ready(self):
        if self._writer_unhealthy or self._recovery_busy() or not self.prepare_done.is_set():
            return False
        mode = self.options['recording_mode']
        cameras = {'microphone_mp3': True, 'seek_only': self.seek_ready,
                   'flir_mp4': bool(self.flir_cams), 'combined': self.cameras_ready}[mode]
        return cameras and (not self.options['microphone'] or self._audio_ready())

    def _check_ready(self):
        if not self.ready():
            raise RuntimeError('Connect the devices selected for this recording mode.')

    def _start(self, source, info):
        if self.closing or self.state != 'idle':
            raise RuntimeError('The previous take is still finalizing.')
        self._check_ready()
        if self._writer_unhealthy or self._recovery_busy():
            raise RuntimeError('Wait for camera cleanup before starting another take.')
        if (self.options['recording_mode'] in ('combined', 'seek_only') and
                self.options['sample_mode'] == 'after_shutter' and self.shutter_settings['automatic']):
            raise RuntimeError('After shutter request sampling requires Custom shutter mode. Apply shutter first.')
        self.shutter_gate.cancel()
        self.next_shutter_ns = None
        request_ns = info['request_monotonic_ns']
        self.core.save_q.configure_streams(self._selected_streams())
        self._recovery_cancel = threading.Event()
        self._pending_drops = {}
        self._shutter_failures = 0
        self._last_shutter_tick_ns = request_ns
        self.journal = None
        self._next_checkpoint_ns = request_ns + 30_000_000_000
        for health in self.health.values():
            health['dropped_frames'] = 0
            health['last_written_ns'] = None
            health['retry_at_ns'] = 0
            health['attempts'] = 0
            health['waiting_since_ns'] = None
            health['gap_start_ns'] = None
        self.take_number += 1
        seek_fps = self.options['sample_hz']
        if self.options['sample_mode'] == 'after_shutter':
            seek_fps = self.shutter_settings['hz']
        elif self.options['sample_mode'] == 'all':
            times = [snap[4] for snap in self.recent_seek]
            intervals = [end - start for start, end in zip(times, times[1:]) if end > start]
            seek_fps = 1e9 / median(intervals) if intervals else 30.0
        self.take = dict(schema_version=2, take_id=self.take_number, label=info.get('label') or self.options['label'],
                         recording_mode=self.options['recording_mode'],
                         sample_mode=self.options['sample_mode'],
                         seek_video_fps=seek_fps,
                         seek_calibration=get_seek_calibration(self.options['seek_calibration']).metadata(),
                         shutter_settings=dict(self.shutter_settings),
                         status='starting', requested_utc=utc_now(),
                         request_monotonic_ns=request_ns, shared_start_monotonic_ns=None,
                         shared_stop_monotonic_ns=None, microphone_enabled=self.options['microphone'],
                         audio_status='pending' if self.options['microphone'] else 'disabled',
                         audio_demo=bool(self.audio and self.audio.config.demo and self.options['microphone']),
                         camera_demo=bool(getattr(self.core, 'demo', False)),
                         first_streams={}, frame_counts={}, frame_id_gaps={}, events=[], errors=[],
                         event_count=0, events_truncated=False,
                         event_log=f'take{self.take_number:02d}_events.jsonl',
                         received_frame_counts={}, written_frame_counts={}, dropped_frame_counts={},
                         recovery_counts={}, has_gaps=False, gaps=[], gap_count=0, segments={},
                         camera_serials={str(cam['index']): cam['serial'] for cam in self.flir_cams},
                         timing_note='Seek and FLIR times are host frame receipt times. Audio uses labelled '
                         'capture-time estimates. Raw device clocks are independent; physical simultaneity is unverified.')
        self._last_flir_ids = {}
        self._last_seek_ns = None
        self.next_seek_ns = None
        self.prepared = False
        self.staged = []
        self.state = 'starting'
        self.finished.clear()
        self.prepare_done.clear()
        self._prepare_abandoned.clear()
        self._ensure_monitor()
        self._event('START_REQUEST', source, info)
        self.core.ui_events.put(('recording_pending',))
        if self.options['recording_mode'] == 'microphone_mp3':
            self._begin_at(request_ns, 'microphone')
        # The first post-click callback can run while Qt reads the controls.
        # Retain a few live frames so that this race cannot move the reference.
        for snap in tuple(self.recent_seek):
            if snap[4] >= request_ns:
                self.on_seek(snap, replay=True)
        threading.Thread(target=self._prepare_worker, name='Prepare camera take', daemon=True).start()

    def _prepare_worker(self):
        journal = None
        try:
            mode = self.take['recording_mode']
            if self._run_mode != mode and self.run_dir is not None:
                if self.writer and self.writer.is_alive():
                    done = threading.Event()
                    self.core.save_q.put(('shutdown', done), timeout=5)
                    if not done.wait(30):
                        raise RuntimeError('Previous camera writer did not close.')
                    self.writer.join(timeout=3)
                    if self.writer.is_alive():
                        raise RuntimeError('Previous camera writer is still running.')
                self.writer = None
                self.run_dir = None
            root = Path(self.options['output']).expanduser().resolve()
            if not root.is_dir():
                raise ValueError('Choose an existing recording folder.')
            minimum = 100_000_000 if mode == 'microphone_mp3' else MIN_FREE_BYTES
            if shutil.disk_usage(root).free < minimum:
                raise OSError('Less than ' + ('100 MB' if mode == 'microphone_mp3' else '1 GiB') +
                              ' free in the recording folder.')
            if self.run_dir is None:
                root = Path(self.options['output']).expanduser().resolve()
                label = safe_label(self.options['label'])
                prefix = 'DEMO_' if self.take['camera_demo'] or self.take['audio_demo'] else ''
                stamp = time.strftime('%Y%m%d_%H%M%S')
                name = f'{prefix}{label}_{stamp}'
                path = root / name
                suffix = 2
                while path.exists():
                    path = root / f'{name}_{suffix:02d}'
                    suffix += 1
                path.mkdir(exist_ok=False)
                self.run_dir = path
                self._run_mode = mode
            if mode != 'microphone_mp3' and self.writer is None:
                ready = threading.Event()
                camera_indices = [] if mode == 'seek_only' else [cam['index'] for cam in self.flir_cams]
                self.writer = threading.Thread(target=self.core.writer_thread,
                                                args=(str(self.run_dir), camera_indices, ready),
                                                daemon=True, name='Camera disk writer')
                self.writer.start()
                if not ready.wait(5) or not self.writer.is_alive():
                    raise RuntimeError('The camera output files could not be initialized.')
            elif mode != 'microphone_mp3' and not self.writer.is_alive():
                raise RuntimeError('The camera writer has stopped. Restart the app before recording again.')
            journal = RecordingJournal(self.run_dir, self.take_number) if mode in ('combined', 'seek_only') else None
            with self.lock:
                if self._prepare_abandoned.is_set():
                    raise RuntimeError('Recording preparation finished after its deadline.')
                self.journal = journal
                if journal:
                    for event in self.take['events']:
                        journal.event(event)
                if mode != 'microphone_mp3':
                    self.core.save_q.put_nowait(('start_take', self.take_number, mode == 'flir_mp4',
                                                self.take['seek_calibration']['name'],
                                                self.options['video_segment_seconds'], self.take['seek_video_fps']))
                self.prepared = True
                if self.take['shared_start_monotonic_ns'] is not None:
                    self._activate_audio()
                for item in self.staged:
                    self.core.save_q.put_reserved(item)
                self.staged.clear()
                self._save_report()
        except Exception as exc:
            self.fault(f'Preparing the take: {exc}')
        finally:
            if journal is not None and self.journal is not journal:
                try:
                    journal.close()
                except Exception:
                    log.exception('Closing abandoned recording journal')
            self.prepare_done.set()

    def _activate_audio(self):
        if self.take['microphone_enabled'] and self.take['audio_status'] == 'pending':
            destination = self.run_dir / 'microphone' / f'take{self.take_number:02d}'
            if self.take['recording_mode'] == 'microphone_mp3':
                self._audio_staging = Path(tempfile.mkdtemp(prefix='.audio_', dir=self.run_dir))
                destination = self._audio_staging / 'capture'
            self.take['audio_path'] = str(destination.relative_to(self.run_dir))
            self.audio.config.label = self.take['label']
            future = self.audio.start_at(self.take_number, self.take['shared_start_monotonic_ns'], destination,
                                         pre_seconds=0 if self.take['recording_mode'] == 'microphone_mp3' else None)
            self.take['audio_status'] = 'requested'
            # Only inspect a completed future here; frame callbacks never wait.
            future.add_done_callback(self._audio_command_done)

    def _audio_command_done(self, future):
        result = future.result()
        if not result['ok']:
            self.fault(result['error'])

    def _enqueue(self, item):
        try:
            if self.prepared:
                self.core.save_q.put_nowait(item)
            else:
                self.core.save_q.reserve(item)
                self.staged.append(item)
            return True
        except queue.Full:
            self.dropped_frame(frame_stream(item), frame_time(item), frame_take(item))
            return False

    def on_seek(self, snap, generation=None, replay=False):
        received_ns = snap[4]
        with self.lock:
            if not all(value is not None for value in snap[1:4]):
                return
            if not replay:
                if not self._stream_seen('seek', received_ns, generation):
                    return
                self.recent_seek.append(snap)
            if self.options['recording_mode'] not in ('combined', 'seek_only'):
                return
            if not self.is_recording or received_ns < self.take['request_monotonic_ns']:
                return
            if self.options['sample_mode'] == 'after_shutter':
                metadata = self.shutter_gate.consume(received_ns, self.take_number,
                                                   valid=all(value is not None for value in snap[1:4]))
                if metadata is None:
                    return
                self._shutter_failures = 0
                snap = (*snap[:3], {**snap[3], **metadata}, received_ns)
            if self.take['shared_start_monotonic_ns'] is None:
                if snap[2] is None or snap[3] is None:
                    return
                self.take['shared_start_monotonic_ns'] = received_ns
                self.take['status'] = 'recording'
                self.state = 'recording'
                self.core.recording.set()
                self.next_seek_ns = received_ns
                self._event('START', 'seek', {'sdk_utc_ns': snap[3].get('timestamp_utc_ns')}, received_ns)
                if self.prepared:
                    self._activate_audio()
                self.core.ui_events.put(('recording_started',))
            t0 = self.take['shared_start_monotonic_ns']
            if received_ns < t0:
                return
            if self.options['sample_mode'] == 'custom' and received_ns < self.next_seek_ns:
                return
            if self.options['sample_mode'] == 'after_shutter':
                self._event('SHUTTER_FRAME', 'seek', metadata, received_ns)
            if self.options['sample_mode'] != 'all' and self._last_seek_ns is not None:
                hz = self.shutter_settings['hz'] if self.options['sample_mode'] == 'after_shutter' else self.options['sample_hz']
                if received_ns - self._last_seek_ns > max(1.5e9 / hz, 1e9 / hz + 100_000_000):
                    self._record_gap('seek', self._last_seek_ns, received_ns, 'sample interval exceeded')
            self._last_seek_ns = received_ns
            if self._enqueue(('seek', snap, self.options['sample_mode'], t0, self.take_number,
                              self._health('seek')['generation'])):
                self._first('seek', received_ns, snap[3].get('timestamp_utc_ns'))
            if self.options['sample_mode'] == 'custom':
                period = max(1, round(1e9 / self.options['sample_hz']))
                self.next_seek_ns = t0 + ((received_ns - t0) // period + 1) * period

    def on_flir(self, cam_index, fid, device_ns, wall, data, received_ns, generation=None):
        with self.lock:
            name = f'flir_cam{cam_index}'
            if not self._stream_seen(name, received_ns, generation):
                return
            if self.options['recording_mode'] in ('microphone_mp3', 'seek_only'):
                return
            if (self.is_recording and self.options['recording_mode'] == 'flir_mp4'
                    and received_ns >= self.take['request_monotonic_ns']
                    and self.take['shared_start_monotonic_ns'] is None):
                self._begin_at(received_ns, 'flir')
            if not self.is_recording or self.take['shared_start_monotonic_ns'] is None:
                return
            t0 = self.take['shared_start_monotonic_ns']
            if received_ns < t0:
                return
            if self._enqueue(('flir', cam_index, fid, device_ns, wall, data, received_ns, t0,
                              self.take_number, self._health(name)['generation'])):
                self._first(name, received_ns, device_ns)
            previous = self._last_flir_ids.get(cam_index)
            if previous is not None and fid > previous + 1:
                self.take['frame_id_gaps'][name] = self.take['frame_id_gaps'].get(name, 0) + fid - previous - 1
                self.take['has_gaps'] = True
            elif previous is not None and fid <= previous:
                self._event('FRAME_ID_RESET', name, dict(previous=previous, frame_id=fid))
                self.take['has_gaps'] = True
            self._last_flir_ids[cam_index] = fid

    def _begin_at(self, host_ns, source):
        self.take['shared_start_monotonic_ns'] = host_ns
        self.take['status'] = self.state = 'recording'
        self.core.recording.set()
        self._event('START', source, {}, host_ns)
        if self.prepared:
            self._activate_audio()
        self.core.ui_events.put(('recording_started',))

    def _first(self, name, host_ns, raw_timestamp):
        self.take['frame_counts'][name] = self.take['frame_counts'].get(name, 0) + 1
        self.take['first_streams'].setdefault(name, dict(host_monotonic_ns=host_ns,
                                                    offset_from_seek_ns=host_ns - self.take['shared_start_monotonic_ns'],
                                                    raw_device_timestamp=raw_timestamp,
                                                    timestamp_source='host_frame_receipt'))

    def _event(self, command, source, info, host_ns=None):
        if self.take is None:
            return
        host_ns = time.perf_counter_ns() if host_ns is None else host_ns
        t0 = self.take['shared_start_monotonic_ns']
        event = {**info, 'command': command, 'source': source,
                                    'host_monotonic_ns': host_ns,
                                    'relative_to_seek_ns': host_ns - t0 if t0 is not None else None,
                                    'received_utc': utc_now()}
        self.take['events'].append(event)
        self.take['event_count'] += 1
        del self.take['events'][:-512]
        self.take['events_truncated'] = self.take['event_count'] > len(self.take['events'])
        if self.journal:
            self.journal.event(event)

    def _stop(self, source, info):
        if not self.is_recording:
            return
        self._recovery_cancel.set()
        self.shutter_gate.cancel()
        self.next_shutter_ns = None
        self.take['shared_stop_monotonic_ns'] = info.get('request_monotonic_ns', time.perf_counter_ns())
        self._event('STOP_REQUEST', source, info, self.take['shared_stop_monotonic_ns'])
        self.state = 'finalizing'
        self.take['status'] = 'finalizing'
        self.core.recording.clear()
        self._flush_drops()
        for stream, health in self.health.items():
            if health['gap_start_ns'] is not None:
                self._record_gap(stream, health['gap_start_ns'], self.take['shared_stop_monotonic_ns'],
                                 'camera unavailable at stop')
                health['gap_start_ns'] = None
        self._save_report()
        self.core.ui_events.put(('recording_finalizing',))
        threading.Thread(target=self._finalize_worker, name='Finalize shared take', daemon=True).start()

    def _finalize_worker(self):
        try:
            if not self.prepare_done.wait(6):
                with self.lock:
                    self._prepare_abandoned.set()
                    self._writer_unhealthy = True
                raise RuntimeError('Timed out waiting for recording preparation.')
            if self.take['microphone_enabled'] and self.take['shared_start_monotonic_ns'] is not None:
                # A stopped take can have latched Seek before preparation finished.
                with self.lock:
                    if self.prepared:
                        self._activate_audio()
                result = self.audio.stop_at(self.take_number, self.take['shared_stop_monotonic_ns']).result(3)
                if not result['ok']:
                    raise RuntimeError(result['error'])
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    status = self.audio.snapshot()
                    if status['error']:
                        self.take['errors'].append(status['error'])
                        break
                    if not status.get('pending') and not status['recording']:
                        break
                    time.sleep(.01)
                else:
                    self.audio.fault('Timed out receiving audio at the shared Stop boundary.')
                    raise RuntimeError('Audio Stop boundary could not be finalized.')
        except Exception as exc:
            self.take['errors'].append(str(exc))
        finally:
            # Audio failure must still drain camera files before another take.
            try:
                if self.writer and self.writer.is_alive():
                    done = threading.Event()
                    self.core.save_q.put(('barrier', done), timeout=5)
                    if not done.wait(30):
                        self._writer_unhealthy = True
                        raise RuntimeError('Camera files did not finish saving within 30 seconds.')
                self._collect_audio_report()
                if self.take['recording_mode'] == 'microphone_mp3' and not self.take['errors']:
                    self._finish_mp3()
            except Exception as exc:
                self.take['errors'].append(str(exc))
            with self.lock:
                for item in self.staged:
                    self.core.save_q.release(item)
                    self.dropped_frame(frame_stream(item), frame_time(item), frame_take(item), 'preparation failed')
                self.staged.clear()
                self._flush_drops()
                expected = ['seek'] + [f"flir_cam{cam['index']}" for cam in self.flir_cams]
                if self.take['recording_mode'] == 'flir_mp4':
                    expected.remove('seek')
                elif self.take['recording_mode'] == 'seek_only':
                    expected = ['seek']
                elif self.take['recording_mode'] == 'microphone_mp3':
                    expected = []
                if self.take['shared_start_monotonic_ns'] is not None:
                    for name in expected:
                        if not self.take['written_frame_counts'].get(name):
                            self.take['errors'].append(f'No recorded frame from {name}.')
                self.take['status'] = ('error' if self.take['errors'] else
                                       'cancelled' if self.take['shared_start_monotonic_ns'] is None else
                                       'complete_with_gaps' if self.take['has_gaps'] else 'complete')
                self.take['stopped_utc'] = utc_now()
                if self.take['shared_start_monotonic_ns'] is None and self.take['microphone_enabled']:
                    self.take['audio_status'] = 'cancelled'
                try:
                    self._save_report()
                except Exception as exc:
                    self.take['errors'].append(f'Saving timing report: {exc}')
                    self.take['status'] = 'error'
            # The journal must drain outside the coordinator lock.
            try:
                if self.journal:
                    self.journal.close()
            except Exception as exc:
                with self.lock:
                    self.take['errors'].append(str(exc))
                    self.take['status'] = 'error'
                if self.run_dir and self.take['recording_mode'] in ('combined', 'seek_only'):
                    try:
                        atomic_json(self.run_dir / f'take{self.take_number:02d}_sync.json', self.take)
                    except Exception:
                        log.exception('Could not save final error report')
            with self.lock:
                self.state = 'idle'
                self.finished.set()
                self.core.ui_events.put(('recording_saved', str(self.run_dir), self.take['status'],
                                         deepcopy(self.take), bool(self.audio and self.audio.config.auto_mp3)))

    def _finish_mp3(self):
        if 'audio_path' not in self.take:
            return
        session = self.run_dir / self.take['audio_path']
        meta = json.loads((session / 'session.json').read_text(encoding='utf-8'))
        wavs = [session / item['file'] for item in meta['segments']]
        if any(path.resolve().parent != session.resolve() for path in wavs):
            raise ValueError('Invalid audio segment path.')
        target = self.run_dir / f'microphone_take{self.take_number:02d}.mp3'
        try:
            export_mp3(wavs, target, time_expansion=1, write_metadata=False)
        except Exception as exc:
            raise RuntimeError(f'MP3 export failed; recoverable WAV retained at {session}: {exc}') from exc
        self.take['mp3_path'] = target.name
        # Only remove the private directory created for this take after export succeeds.
        staging = self._audio_staging.resolve()
        if staging.parent != self.run_dir.resolve() or not staging.name.startswith('.audio_'):
            raise RuntimeError('Unexpected temporary audio directory; retained for recovery.')
        shutil.rmtree(staging)
        self._audio_staging = None

    def _collect_audio_report(self):
        if not self.take['microphone_enabled'] or 'audio_path' not in self.take:
            return
        path = self.run_dir / self.take['audio_path']
        manifest = path / 'session.json'
        if not manifest.exists():
            self.take['audio_status'] = 'error'
            self.take['errors'].append('Audio session manifest is missing.')
            return
        meta = json.loads(manifest.read_text(encoding='utf-8'))
        self.take['audio_status'] = meta['status']
        self.take['audio_samples'] = meta['recorded_samples']
        self.take['separate_pretrigger_samples'] = meta.get('separate_pretrigger_samples', 0)
        first = meta.get('first_saved_monotonic_ns')
        if first is not None:
            self.take['first_streams']['microphone'] = dict(host_monotonic_ns=first,
                                                          offset_from_seek_ns=first - self.take['shared_start_monotonic_ns'],
                                                          timestamp_source=meta.get('timestamp_source'),
                                                          file_sample=0)
        if meta['status'] != 'complete' or not meta['recorded_samples']:
            self.take['errors'].extend(meta.get('errors') or ['Audio contains no completed samples.'])

    def _save_report(self):
        if not self.journal or not self.take or self.take['recording_mode'] not in ('combined', 'seek_only'):
            return
        self.take['diagnostic_events_dropped'] = self.journal.dropped_events
        self.take['stream_health'] = deepcopy(self.health)
        self.journal.checkpoint(deepcopy(self.take))

    def fault(self, message):
        log.error('Recording failure: %s', message)
        with self.lock:
            self.armed = False
            if self.take and self.state != 'idle':
                if message not in self.take['errors']:
                    self.take['errors'].append(message)
                self._stop('system', {'error': message})
            self.core.ui_events.put(('message', message))

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.request('DISARM')
        threading.Thread(target=self._shutdown_worker, name='Camera shutdown', daemon=True).start()

    def _shutdown_worker(self):
        try:
            self.connect_done.wait()
            self.finished.wait()
            self._recovery_cancel.set()
            self._monitor_stop.set()
            if self._monitor:
                self._monitor.join(3)
            self.core.stop_all.set()
            for worker in tuple(self._recovery_threads.values()):
                if worker.ident is not None:
                    worker.join(timeout=3)
            for worker in tuple(self.workers):
                worker.join(timeout=3)
            seek_worker = getattr(self.core, 'seek_worker', None)
            if seek_worker:
                seek_worker.join(timeout=3)
            event_worker = getattr(self.core, 'seek_event_worker', None)
            if event_worker:
                event_worker.join(timeout=3)
            if self.writer and self.writer.is_alive():
                done = threading.Event()
                self.core.save_q.put(('shutdown', done), timeout=5)
                if not done.wait(30):
                    self.core.ui_events.put(('message', 'Camera writer did not finish shutdown within 30 seconds.'))
                self.writer.join(timeout=3)
        except Exception as exc:
            self.core.ui_events.put(('message', f'Camera shutdown error: {exc}'))
        finally:
            try:
                self._release_cameras()
            except Exception as exc:
                self.core.ui_events.put(('message', f'Camera cleanup error: {exc}'))
            finally:
                # The GUI must finish closing even if a vendor SDK rejects cleanup.
                self.core.ui_events.put(('shutdown_complete',))

    def _release_cameras(self):
        self.core.recording.clear()
        event_worker = getattr(self.core, 'seek_event_worker', None)
        if (self._recovery_busy() or any(worker.is_alive() for worker in self.workers)
                or (event_worker and event_worker.is_alive())):
            self.core.ui_events.put(('message', 'An SDK worker is still busy; its resources are retained '
                                    'until process exit to avoid releasing a camera in use.'))
            return
        for message in release_flir_cameras(self.flir_cams, self.cam_list, self.system):
            self.core.ui_events.put(('message', message))
        self.cam_list = self.system = None
        with getattr(self.core, 'seek_control_lock', nullcontext()):
            self.core.seek_session_generation = -1
            if self.core.active_seek_cam:
                try:
                    self.core.seek.seekcamera_capture_session_stop(self.core.active_seek_cam)
                except Exception:
                    log.exception('Seek capture cleanup failed')
                self.core.active_seek_cam = None
            if self.manager:
                try:
                    self.core.seek.seekcamera_manager_destroy(self.core.ctypes.byref(self.manager))
                except Exception:
                    log.exception('Seek manager cleanup failed')
                self.manager = None
        self.cameras_ready = False
        self.seek_ready = False

    def seek_disconnected(self):
        self.camera_fault('seek', 'Seek disconnected.', disconnected=True)

    def shutter_tick(self):
        """Called by the shutter worker; never hold the coordinator lock in SDK calls."""
        with self.lock:
            self._last_shutter_tick_ns = time.perf_counter_ns()
            if self.options['sample_mode'] != 'after_shutter':
                return False
            if not self.is_recording or self.options['recording_mode'] not in ('combined', 'seek_only'):
                return True
            if self._health('seek')['state'] in ('recovering', 'disconnected'):
                return True
            if self.shutter_gate.expired():
                self.shutter_gate.cancel()
                self._shutter_failure('No valid Seek frame arrived within five seconds of the shutter request.')
                self.next_shutter_ns = time.perf_counter_ns() + max(1, round(1e9 / self.shutter_settings['hz']))
                return True
            now = time.perf_counter_ns()
            if self.next_shutter_ns is not None and now < self.next_shutter_ns:
                return True
            token = self.shutter_gate.begin(self.take_number)
            self.next_shutter_ns = now + max(1, round(1e9 / self.shutter_settings['hz']))
            if token is None:
                return True
            take_id = self.take_number
        try:
            metadata = self.shutter_gate.request(token, self.core.trigger_seek_shutter)
            with self.lock:
                if metadata and self.take_number == take_id and self.is_recording:
                    self._event('SHUTTER_REQUEST', 'seek', metadata,
                                metadata['shutter_request_monotonic_ns'])
        except Exception as exc:
            with self.lock:
                if self.take_number == take_id and self.is_recording:
                    self._shutter_failure(str(exc))
        return True

    def _shutter_failure(self, message):
        self._shutter_failures += 1
        self.camera_fault('seek', message)
        if self._shutter_failures >= 2:
            self._health('seek')['force_recovery'] = True

    def apply_shutter(self, label, hz=1.0):
        with self.lock:
            if self.is_recording and self.options['sample_mode'] == 'after_shutter':
                raise RuntimeError('Stop the take before changing shutter settings.')
            automatic = label == 'Automatic'
            if not math.isfinite(hz) or hz <= 0:
                raise ValueError('Custom shutter rate must be positive.')
            core = self.core
        core.configure_seek_shutter(core.SEEKCAMERA_SHUTTER_MODE_AUTO if automatic else
                                    core.SEEKCAMERA_SHUTTER_MODE_MANUAL, not automatic, hz)
        with self.lock:
            self.shutter_settings = dict(automatic=automatic, hz=hz)

    def camera_action(self, action):
        with getattr(self.core, 'seek_control_lock', nullcontext()):
            self._camera_action_locked(action)

    def _camera_action_locked(self, action):
        core = self.core
        cam = core.active_seek_cam
        if not cam:
            raise RuntimeError('Seek is not connected.')
        value = core.c_int()
        if action == 'palette':
            result = core.seek.seekcamera_get_color_palette(cam, core.ctypes.byref(value))
            if result == 0:
                selected = (value.value + 1) % core.SEEKCAMERA_COLOR_PALETTE_USER_0
                result = core.seek.seekcamera_set_color_palette(cam, selected)
        elif action == 'pipeline':
            result = core.seek.seekcamera_get_pipeline_mode(cam, core.ctypes.byref(value))
            if result == 0:
                selected = (value.value + 1) % 3
                result = core.seek.seekcamera_set_pipeline_mode(cam, selected)
        elif action == 'sharpen':
            result = core.seek.seekcamera_get_pipeline_mode(cam, core.ctypes.byref(value))
            if result != 0:
                raise RuntimeError(f'Could not read Seek pipeline (error {result}).')
            if value.value == 2:
                raise RuntimeError('Sharpening cannot change in the SeekVision pipeline.')
            result = core.seek.seekcamera_get_filter_state(cam, core.SEEKCAMERA_FILTER_SHARPEN_CORRECTION,
                                                          core.ctypes.byref(value))
            if result == 0:
                selected = (value.value + 1) % 2
                result = core.seek.seekcamera_set_filter_state(cam, core.SEEKCAMERA_FILTER_SHARPEN_CORRECTION,
                                                              selected)
        else:
            raise ValueError('Unknown Seek camera action.')
        if result != 0:
            raise RuntimeError(f'Seek rejected {action} (error {result}).')
        if hasattr(core, 'seek_preferences'):
            core.seek_preferences[action] = selected
