"""Timestamp-targeted audio recording for the shared camera take controller.

The original standalone FIFO recorder remains unchanged. Times here are host
estimates, never a claim that independent USB devices share a hardware clock.
"""
from __future__ import annotations

import csv
import math
import queue
import time
import wave
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path

from .core import AudioBlock, RecorderEngine, SessionWriter, atomic_json, utc_now


@dataclass(frozen=True)
class TimedBlock:
    block: AudioBlock
    first_ns: int
    source: str
    clock_bracket_ns: int | None

    @property
    def end_ns(self):
        return self.first_ns + self.block.frames * 5000

    def slice(self, first=0, end=None):
        end = self.block.frames if end is None else end
        b = self.block
        clipped = AudioBlock(b.pcm[first * 2:end * 2], b.start + first,
                             None if b.adc_time is None else b.adc_time + first / 200_000,
                             b.callback_time, b.received_ns)
        return TimedBlock(clipped, self.first_ns + first * 5000,
                          self.source, self.clock_bracket_ns)


class AlignedSessionWriter(SessionWriter):
    def __init__(self, config, boundary, info, destination):
        self.timing_file = None
        super().__init__(replace(config, output=destination.parent, pre_seconds=0),
                         boundary, [], info, config.label, destination=destination)
        self.meta.update(take_id=info['take_id'], shared_start_monotonic_ns=info['target_ns'],
                         timing_note='Audio is trimmed using host capture-time estimates. '
                         'Independent USB clocks and driver latency prevent a hardware simultaneity claim. '
                         'Latency/receipt estimates anchor a nominal 200 kHz timeline; their clock drift is uncorrected.',
                         separate_pretrigger_samples=0, separate_pretrigger_file=None,
                         separate_pretrigger_requested_seconds=config.pre_seconds,
                         first_saved_monotonic_ns=None)
        try:
            self.timing_file = (self.path / 'audio_timing.csv').open('w', newline='', encoding='utf-8')
            self.timing_csv = csv.writer(self.timing_file)
            self.timing_csv.writerow(['stream_sample', 'file_sample', 'frames',
                                     'first_sample_host_estimate_ns', 'relative_to_seek_ns',
                                     'timestamp_source', 'clock_bracket_width_ns',
                                     'adc_time_seconds', 'callback_time_seconds', 'host_received_monotonic_ns'])
            atomic_json(self.path / 'session.json', self.meta)
        except Exception:
            self.close('error', 'Could not initialize audio timing files.')
            raise

    def write_timed(self, timed):
        b = timed.block
        if self.meta['first_saved_monotonic_ns'] is None:
            self.meta.update(first_saved_monotonic_ns=timed.first_ns,
                             start_offset_ns=timed.first_ns - self.meta['shared_start_monotonic_ns'],
                             timestamp_source=timed.source)
        self.timing_csv.writerow([b.start, self.total, b.frames, timed.first_ns,
                                 timed.first_ns - self.meta['shared_start_monotonic_ns'],
                                 timed.source, timed.clock_bracket_ns,
                                 b.adc_time, b.callback_time, b.received_ns])
        self.write(b)
        self.timing_file.flush()

    def close(self, status='complete', error=None):
        if self.timing_file is not None:
            try:
                self.timing_file.close()
            except Exception as exc:
                error = '; '.join(filter(None, (error, f'Closing audio timing log: {exc}')))
            finally:
                self.timing_file = None
        return super().close(status, error)

    def truncate_to(self, samples):
        """A STOP can arrive after its containing block was already written."""
        samples = max(0, min(samples, self.total))
        if samples == self.total:
            return
        self._close_segment()
        kept = []
        for segment in self.segments:
            count = min(segment['samples'], max(0, samples - segment['first_file_sample']))
            path = self.path / segment['file']  # Names created by this writer, never imported manifests.
            if count == 0:
                path.unlink()
            else:
                if count != segment['samples']:
                    import struct
                    with path.open('r+b') as handle:
                        handle.seek(4)
                        handle.write(struct.pack('<I', 36 + count * 2))
                        handle.seek(40)
                        handle.write(struct.pack('<I', count * 2))
                        handle.truncate(44 + count * 2)
                segment['samples'] = count
                kept.append(segment)
        self.segments[:] = kept
        self.total = samples
        for handle, filename in ((self.blocks, 'audio_blocks.csv'),
                                 (self.timing_file, 'audio_timing.csv')):
            handle.flush()
            path = self.path / filename
            with path.open(newline='', encoding='utf-8') as reader:
                rows = list(csv.reader(reader))
            clipped = [rows[0]]
            for row in rows[1:]:
                if int(row[1]) >= samples:
                    continue
                row[2] = str(min(int(row[2]), samples - int(row[1])))
                clipped.append(row)
            handle.seek(0)
            handle.truncate()
            csv.writer(handle).writerows(clipped)
            handle.flush()


class SynchronizedAudioEngine(RecorderEngine):
    """A single-owner writer with bounded history and asynchronous target times."""

    def __init__(self, config, queue_blocks=512):
        self.history = deque()
        self.history_frames = 0
        self.pending_start = None
        self.pending_stop = None
        self.last_written = None
        self.clock_offset_ns = None
        self.clock_bracket_ns = None
        self.clock_anchors = []
        self.receipt_origin_ns = None
        self.latency_origin_ns = None
        super().__init__(config, queue_blocks)

    def set_clock_mapping(self, offset_ns, bracket_ns, stream_time):
        with self._lock:
            self.clock_offset_ns = offset_ns
            self.clock_bracket_ns = bracket_ns
            self.clock_anchors.append(dict(host_ns=time.perf_counter_ns(),
                                           stream_time_seconds=stream_time,
                                           offset_ns=offset_ns, bracket_width_ns=bracket_ns))
            self.clock_anchors = self.clock_anchors[-120:]

    def _submit(self, command, **info):
        future = Future()
        if self._fault or not self.thread.is_alive():
            future.set_result({'ok': False, 'error': self._fault or 'Microphone disconnected.'})
            return future
        info.update(source='manual', received_utc=utc_now(), received_monotonic_ns=time.perf_counter_ns())
        try:
            self.inbox.put_nowait((command, info, future))
        except queue.Full:
            self.fault('Audio command queue overflowed.')
            future.set_result({'ok': False, 'error': 'Audio command queue overflowed.'})
        return future

    def start_at(self, take_id, t0_ns, output_dir, *, pre_seconds=None):
        return self._submit('SYNC_START', take_id=take_id, target_ns=t0_ns,
                            destination=str(output_dir), pre_seconds=pre_seconds)

    def stop_at(self, take_id, t1_ns):
        return self._submit('SYNC_STOP', take_id=take_id, target_ns=t1_ns)

    def _stamp(self, block):
        with self._lock:
            offset, bracket = self.clock_offset_ns, self.clock_bracket_ns
        adc, callback = block.adc_time, block.callback_time
        if (offset is not None and adc is not None and callback is not None
                and math.isfinite(adc) and math.isfinite(callback)
                and 0 <= callback - adc <= 3):
            first = round(adc * 1e9) + offset
            quality = 'driver_capture_estimate'
        elif offset is not None and callback is not None and math.isfinite(callback):
            latency = self.config.device.get('driver_reported_input_latency_seconds')
            if latency is None or not math.isfinite(latency) or latency < 0:
                latency = block.frames / self.config.rate
            if self.latency_origin_ns is None:
                self.latency_origin_ns = round((callback - latency) * 1e9) + offset - block.start * 5000
            # Drivers can deliver several blocks with one callback timestamp.
            # A nominal sample timeline preserves the order of those samples.
            first = self.latency_origin_ns + block.start * 5000
            quality = 'driver_latency_estimate'
        else:
            # MME exposes receipt times only. Anchor a nominal sample timeline;
            # unknown USB/driver latency is explicitly retained in the quality label.
            if self.receipt_origin_ns is None:
                self.receipt_origin_ns = block.received_ns - (block.start + block.frames) * 5000
            first = self.receipt_origin_ns + block.start * 5000
            quality = 'synthetic' if self.config.demo else 'host_receipt_estimate_latency_unknown'
        self._update(timestamp_source=quality, clock_bracket_width_ns=bracket)
        return TimedBlock(block, first, quality, bracket)

    def _handle(self, command, info):
        if command == 'SYNC_START':
            if self.session or self.pending_start:
                return {'ok': False, 'error': 'Audio take already active.'}
            self.pending_start = info
            self.pending_stop = None
            self._update(pending=True, target_monotonic_ns=info['target_ns'])
            self._begin_if_ready()
            return {'ok': True, **self.snapshot()}
        if command == 'SYNC_STOP':
            active = self.pending_start or (self.session.meta if self.session else None)
            if active and active['take_id'] != info['take_id']:
                return {'ok': False, 'error': 'Audio take ID mismatch.'}
            self.pending_stop = info
            if self.session and self.history and self.history[-1].end_ns >= info['target_ns']:
                boundary = self.last_written
                for timed in self.history:
                    if timed.end_ns >= info['target_ns']:
                        skip = max(0, min(timed.block.frames,
                                          (info['target_ns'] - timed.first_ns + 4999) // 5000))
                        boundary = timed.block.start + skip
                        break
                first_sample = self.session.meta['first_stream_sample']
                self.session.truncate_to(boundary - first_sample)
                self.last_written = min(self.last_written, boundary)
                self._finish(info)
            return {'ok': True, **self.snapshot()}
        if command == 'CLOSE':
            self.pending_start = None
            self._update(pending=False)
        return super()._handle(command, info)

    def _begin_if_ready(self):
        if self.pending_start is None or not self.history:
            return
        info = self.pending_start
        target = info['target_ns']
        if target < self.history[0].first_ns:
            raise RuntimeError('The requested Seek start is older than available audio history.')
        chosen = next((b for b in self.history if b.end_ns > target), None)
        if chosen is None:
            return
        skip = max(0, (target - chosen.first_ns + 4999) // 5000)
        boundary = chosen.block.start + skip
        destination = Path(info['destination'])
        config = self.config if info.get('pre_seconds') is None else replace(
            self.config, pre_seconds=info['pre_seconds'])
        self.session = AlignedSessionWriter(config, boundary, info, destination)
        self.pending_start = None
        self.last_written = boundary
        pre_begin = max(self.history[0].block.start,
                        boundary - round(config.pre_seconds * config.rate))
        if pre_begin < boundary:
            with wave.open(str(destination / 'pretrigger.wav'), 'wb') as wav:
                wav.setparams((1, 2, self.config.rate, 0, 'NONE', 'not compressed'))
                for timed in self.history:
                    b = timed.block
                    lo, hi = max(pre_begin, b.start), min(boundary, b.start + b.frames)
                    if hi > lo:
                        wav.writeframes(b.pcm[(lo - b.start) * 2:(hi - b.start) * 2])
            self.session.meta.update(separate_pretrigger_samples=boundary - pre_begin,
                                     separate_pretrigger_file='pretrigger.wav',
                                     separate_pretrigger_first_stream_sample=pre_begin)
        self._update(pending=False, recording=True, session_path=str(destination), recorded_samples=0)
        self.notices.put({'type': 'started', 'path': str(destination)})
        for timed in self.history:
            if self.session is None:
                break
            self._write_available(timed)

    def _write_available(self, timed):
        if self.session is None:
            return
        b = timed.block
        lo = max(0, self.last_written - b.start)
        hi = b.frames
        if self.pending_stop:
            hi = min(hi, max(0, (self.pending_stop['target_ns'] - timed.first_ns + 4999) // 5000))
        if hi > lo:
            self.session.write_timed(timed.slice(lo, hi))
            self.last_written = b.start + hi
            self._update(recorded_samples=self.session.total)
        if self.pending_stop and timed.end_ns >= self.pending_stop['target_ns']:
            self._finish(self.pending_stop)

    def _audio(self, block):
        if len(block.pcm) % 2 or block.start != self.cursor:
            raise RuntimeError(f'Audio discontinuity: expected sample {self.cursor}, received {block.start}.')
        timed = self._stamp(block)
        self.history.append(timed)
        self.history_frames += block.frames
        while len(self.history) > 1 and self.history_frames - self.history[0].block.frames >= 2_000_000:
            self.history_frames -= self.history.popleft().block.frames
        self.cursor += block.frames
        self._update(samples=self.cursor)
        self._begin_if_ready()
        self._write_available(timed)
        try:
            self.preview.put_nowait(block)
        except queue.Full:
            try:
                self.preview.get_nowait()
            except queue.Empty:
                pass
            self.preview.put_nowait(block)
            self._update(preview_drops=self.snapshot()['preview_drops'] + 1)

    def _finish(self, info, error=None):
        self.pending_start = None
        self._update(pending=False)
        if self.session is None:
            return None
        self.session.meta.update(shared_stop_monotonic_ns=info.get('target_ns'),
                                 clock_anchors=list(self.clock_anchors))
        session, self.session = self.session, None
        session.event('ERROR' if error else 'STOP', info, self.last_written or self.cursor)
        path = session.close(error=error)
        self.pending_stop = None
        self._update(recording=False, recorded_samples=session.total)
        self.notices.put({'type': 'saved', 'path': str(path), 'error': error,
                          'auto_mp3': self.config.auto_mp3 and not error})
        return path


def calibrate_source_clock(source, engine):
    """Read PortAudio only from a control thread, never from its callback."""
    stream = getattr(source, 'stream', None)
    if stream is None:
        return
    best = None
    for _ in range(20):
        before = time.perf_counter_ns()
        stream_time = stream.time
        after = time.perf_counter_ns()
        item = (after - before, (before + after) // 2 - round(stream_time * 1e9), stream_time)
        if best is None or item[0] < best[0]:
            best = item
    engine.set_clock_mapping(best[1], best[0], best[2])
