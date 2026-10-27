"""Independent disk owners for Seek thermal data and segmented camera videos."""
from __future__ import annotations

import csv
from pathlib import Path
import queue
import threading
import time

import cv2
import numpy as np

from camera_reliability import frame_stream, frame_time, frame_take, log
from seek_calibration import get_seek_calibration

FLIR_COLUMNS = ['idx', 'frame_id', 'cam_ts_ns', 'pc_time', 'take_id',
                'host_received_monotonic_ns', 'relative_to_seek_ns', 'timestamp_source',
                'connection_generation', 'video_file', 'part_frame_index']
SEEK_COLUMNS = ['idx', 'file', 'take_id', 'sdk_utc_ns', 'fpa_frame_count',
                'host_received_monotonic_ns', 'relative_to_seek_ns', 'timestamp_source',
                'calibration', 'connection_generation', 'part_frame_index']


def _csv_file(path, columns=None):
    handle = path.open('x', newline='', encoding='utf-8')
    writer = csv.writer(handle)
    if columns:
        writer.writerow(columns)
    return handle, writer


class _CameraVideoWriter:
    def __init__(self, core, root, stream, log_name, columns):
        self.core, self.root = core, root
        self.owner = core.coordinator
        self.stream, self.log_name, self.columns = stream, log_name, columns
        self.fps = 30.0
        self.log_file = self.log_csv = None
        self.video = self.part_log = None
        self.segment = None
        self.count = 0
        self.last_id = None
        self.take_id = 0
        self.failed_take = False

    def start(self, item):
        self.finish()
        self.take_id, self.media_only = item[1:3]
        self.seconds = item[4]
        self.part_number = 0
        self.failed_take = False
        self.encoder_failures = 0
        self.retry_at = 0
        if not self.media_only and self.log_file is None:
            self.log_file, self.log_csv = _csv_file(self.root / self.log_name, self.columns)

    def _open(self, bgr, received, t0, generation):
        folder = self.root if self.media_only else self.root / 'video_parts'
        folder.mkdir(exist_ok=True)
        while True:
            self.part_number += 1
            stem = f'{self.stream}_take{self.take_id:02d}_part{self.part_number:04d}'
            self.path = folder / (stem + '.mp4')
            self.partial = folder / (stem + '.partial.mp4')
            if self.path.exists() or self.partial.exists() or (folder / (stem + '.csv')).exists():
                continue
            try:
                self.partial.open('xb').close()
                break
            except FileExistsError:
                continue
        height, width = bgr.shape[:2]
        self.shape = bgr.shape
        self.bucket = int((received - t0) // max(1, round(self.seconds * 1e9)))
        self.generation = generation
        self.last_id = None
        self.segment = dict(stream=self.stream, take_id=self.take_id, part=self.part_number,
                            file=str(self.path.relative_to(self.root)),
                            partial_file=str(self.partial.relative_to(self.root)),
                            connection_generation=generation, first_host_monotonic_ns=received,
                            last_host_monotonic_ns=received, frames=0, fps=self.fps, status='open')
        self.video = cv2.VideoWriter(str(self.partial), cv2.VideoWriter_fourcc(*'mp4v'), self.fps, (width, height))
        if not self.video.isOpened():
            raise RuntimeError(f'Could not initialize video encoder for {self.path}.')
        if not self.media_only:
            self.part_log, self.part_csv = _csv_file(folder / (stem + '.csv'), self.columns)
            self.segment['timing_csv'] = str((folder / (stem + '.csv')).relative_to(self.root))
        self.owner.segment_update(self.segment)

    def _write_video(self, bgr, received, t0, take_id, generation, fid):
        if self.failed_take:
            self.owner.dropped_frame(self.stream, received, take_id, 'writer failed')
            return False
        if time.monotonic() < self.retry_at:
            self.owner.dropped_frame(self.stream, received, take_id, 'encoder restarting')
            return False
        bucket = int((received - t0) // max(1, round(self.seconds * 1e9)))
        if self.video is not None and (bucket != self.bucket or generation != self.generation or
                bgr.shape != self.shape or (self.last_id is not None and fid is not None and fid <= self.last_id)):
            self.finish()
        try:
            if self.video is None:
                self._open(bgr, received, t0, generation)
            self.video.write(bgr)
        except Exception as exc:
            self._abandon()
            self.encoder_failures += 1
            self.owner.dropped_frame(self.stream, received, take_id, f'encoder error: {exc}')
            self.retry_at = time.monotonic() + min(5, self.encoder_failures)
            log.exception('%s encoder failed', self.stream)
            if self.encoder_failures >= 3:
                raise OSError(f'{self.stream}: video encoder failed three times: {exc}') from exc
            return False
        return True

    def _frame_written(self, received, take_id, fid):
        # Finalization independently checks the encoder's container frame count.
        self.segment['frames'] += 1
        self.segment['last_host_monotonic_ns'] = received
        self.last_id = fid
        self.count += 1
        self.owner.frame_written(self.stream, received, take_id)

    def flush(self):
        for handle in (self.log_file, self.part_log):
            if handle:
                handle.flush()

    def _abandon(self):
        failures = []
        if self.video is not None:
            try:
                self.video.release()
            except Exception as exc:
                failures.append(str(exc))
            self.video = None
        if self.part_log is not None:
            try:
                self.part_log.close()
            except Exception as exc:
                failures.append(str(exc))
            self.part_log = None
        if self.segment:
            self.segment['status'] = 'error'
            self.owner.segment_update(self.segment)
            self.segment = None
        if failures:
            raise OSError('; '.join(failures))

    def finish(self):
        if self.video is not None:
            try:
                self.video.release()
                self.video = None
                if self.part_log:
                    self.part_log.close()
                    self.part_log = None
                capture = cv2.VideoCapture(str(self.partial))
                try:
                    count = round(capture.get(cv2.CAP_PROP_FRAME_COUNT)) if capture.isOpened() else -1
                finally:
                    capture.release()
                if count != self.segment['frames'] or count <= 0:
                    raise OSError(f'Video part verification failed: {self.partial} '
                                  f'contains {count} frames, expected {self.segment["frames"]}.')
                if self.path.exists():
                    raise FileExistsError(f'Refusing to overwrite {self.path}.')
                self.partial.rename(self.path)
                self.segment['status'] = 'complete'
                self.segment['bytes'] = self.path.stat().st_size
                self.owner.segment_update(self.segment)
                self.segment = None
                self.encoder_failures = 0
            except Exception:
                self._abandon()
                raise
        self.flush()

    def close(self):
        try:
            self.finish()
        finally:
            if self.log_file:
                self.log_file.close()


class FlirWriter(_CameraVideoWriter):
    def __init__(self, core, root, index):
        super().__init__(core, root, f'flir_cam{index}', f'flir_log_cam{index}.csv', FLIR_COLUMNS)

    def write(self, item):
        _, idx, fid, device_ns, wall, data, received, t0, take_id, generation = item
        if data.ndim == 2:
            pixels = data if data.dtype == np.uint8 else (data / 256).astype(np.uint8)
            bgr = cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGR)
        else:
            bgr = data
        if not self._write_video(bgr, received, t0, take_id, generation, fid):
            return
        row = [self.count, fid, device_ns, f'{wall:.9f}', take_id, received,
               received - t0, 'host_frame_receipt', generation,
               self.segment['file'], self.segment['frames']]
        if not self.media_only:
            self.log_csv.writerow(row)
            self.part_csv.writerow(row)
        self._frame_written(received, take_id, fid)


class SeekWriter(_CameraVideoWriter):
    def __init__(self, core, root):
        super().__init__(core, root, 'seek', 'seek_log.csv', SEEK_COLUMNS)
        self.thermal = None

    def start(self, item):
        super().start(item)
        self.calibration = get_seek_calibration(item[3])
        self.fps = item[5]

    def write(self, item):
        _, snap, mode, t0, take_id, generation = item
        wall, bgra, raw, header, received = snap
        if self.media_only:
            return
        if raw is None or header is None:
            raise RuntimeError('Seek thermal pixels or metadata are missing.')
        bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
        # MPEG-4 needs even dimensions; retain edge pixels instead of cropping.
        height, width = bgr.shape[:2]
        if height % 2 or width % 2:
            bgr = cv2.copyMakeBorder(bgr, 0, height % 2, 0, width % 2, cv2.BORDER_REPLICATE)
        fid = header.get('fpa_frame_count')
        if not self._write_video(bgr, received, t0, take_id, generation, fid):
            return
        if self.thermal is None:
            self.thermal, self.thermal_csv = _csv_file(self.root / 'seek_thermal.csv')
        calibration = self.calibration
        metadata = [f'take_id={take_id}', f'host_received_monotonic_ns={received}',
                    f'relative_to_seek_ns={received - t0}', f'calibration={calibration.name}',
                    f'calibration_scale={calibration.scale}', f'calibration_offset_c={calibration.offset_c}',
                    'temperature_units=C', f'connection_generation={generation}']
        for key, value in header.items():
            if key == 'line_stride':
                continue
            if isinstance(value, (list, tuple)):
                metadata.extend([f'{key}=' + '.'.join(map(str, value))] if key == 'firmware_version'
                                else [key] * len(value))
            else:
                metadata.append(f'{key}={value}')
        metadata.extend(['blank'] * max(0, raw.shape[1] - len(metadata)))
        self.thermal_csv.writerow(metadata + [''])
        for row in calibration.apply(raw):
            self.thermal_csv.writerow([f'{float(value):.1f}' for value in row] + [''])
        row = [self.count, self.segment['file'], take_id,
            header.get('timestamp_utc_ns'), header.get('fpa_frame_count'), received, received - t0,
            'host_frame_receipt', calibration.name, generation, self.segment['frames']]
        self.log_csv.writerow(row)
        self.part_csv.writerow(row)
        self._frame_written(received, take_id, fid)

    def flush(self):
        super().flush()
        if self.thermal:
            self.thermal.flush()

    def close(self):
        try:
            super().close()
        finally:
            if self.thermal:
                self.thermal.close()


def _sink_worker(core, sink, pending):
    last_flush = time.monotonic()
    try:
        while True:
            try:
                item = pending.get(timeout=.25)
            except queue.Empty:
                item = None
            kind = item[0] if item else None
            try:
                if kind == 'start_take':
                    sink.start(item)
                elif kind in ('barrier', 'shutdown'):
                    sink.finish()
                elif item:
                    sink.write(item)
                if time.monotonic() - last_flush >= 1:
                    sink.flush()
                    last_flush = time.monotonic()
            except Exception as exc:
                sink.failed_take = True
                log.exception('%s writer failed', sink.stream)
                core.coordinator.fault(f'{sink.stream} output error: {exc}')
                if kind in ('seek', 'flir'):
                    core.coordinator.dropped_frame(sink.stream, frame_time(item), frame_take(item), 'output error')
            finally:
                if kind in ('seek', 'flir'):
                    core.save_q.release(item)
                elif kind in ('barrier', 'shutdown'):
                    item[1].set()
            if kind == 'shutdown':
                return
    finally:
        try:
            sink.close()
        except Exception as exc:
            core.coordinator.fault(f'{sink.stream} output cleanup: {exc}')


def write_camera_files(core, run_dir, cam_indices, ready_event=None):
    """Dispatch in FIFO order; reservations survive dispatch until each sink finishes."""
    workers = {}
    try:
        root = Path(run_dir)
        root.mkdir(parents=True, exist_ok=True)
        sinks = [SeekWriter(core, root)] + [FlirWriter(core, root, idx) for idx in cam_indices]
        for sink in sinks:
            pending = queue.Queue()  # FrameBuffer owns the byte budget for this queue too.
            worker = threading.Thread(target=_sink_worker, args=(core, sink, pending),
                                      name=f'Write {sink.stream}', daemon=True)
            workers[sink.stream] = pending, worker
            worker.start()
        if ready_event:
            ready_event.set()
        while True:
            try:
                item = core.save_q.get(timeout=.5)
            except queue.Empty:
                if any(not worker.is_alive() for _, worker in workers.values()):
                    raise RuntimeError('A camera output worker stopped unexpectedly.')
                continue
            kind = item[0]
            if kind in ('seek', 'flir'):
                name = frame_stream(item)
                if name not in workers or not workers[name][1].is_alive():
                    core.save_q.release(item)
                    raise RuntimeError(f'No output worker for {name}.')
                workers[name][0].put_nowait(item)
            elif kind == 'start_take':
                for pending, _ in workers.values():
                    pending.put_nowait(item)
            elif kind in ('barrier', 'shutdown'):
                acknowledgements = []
                for pending, _ in workers.values():
                    done = threading.Event()
                    pending.put_nowait((kind, done))
                    acknowledgements.append(done)
                deadline = time.monotonic() + 25
                if not all(done.wait(max(0, deadline - time.monotonic())) for done in acknowledgements):
                    raise RuntimeError('Camera output workers did not finish their pending files.')
                if kind == 'shutdown':
                    for _, worker in workers.values():
                        worker.join(2)
                    if any(worker.is_alive() for _, worker in workers.values()):
                        raise RuntimeError('Camera output worker did not close.')
                item[1].set()
                if kind == 'shutdown':
                    return
    except Exception as exc:
        core.coordinator._writer_unhealthy = True
        core.coordinator.fault(f'Camera disk writer: {exc}')
        log.exception('Camera dispatcher failed')
    finally:
        if ready_event:
            ready_event.set()
        for pending, worker in workers.values():
            if worker.is_alive():
                pending.put_nowait(('shutdown', threading.Event()))
