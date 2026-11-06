"""Bounded frame buffering, durable diagnostics, and recording power management."""
from __future__ import annotations

import atexit
import ctypes
import faulthandler
import json
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading
import time

from ultramic_studio.core import atomic_json

MIN_FREE_BYTES = 1024 ** 3
WARN_FREE_BYTES = 10 * 1024 ** 3
FRAME_BUFFER_BYTES = 256 * 1024 ** 2
log = logging.getLogger('ultramic.recording')
_listener = None
_crash_file = None


def frame_stream(item):
    return f'flir_cam{item[1]}' if item[0] == 'flir' else 'seek'


def frame_time(item):
    return item[6] if item[0] == 'flir' else item[1][4]


def frame_take(item):
    return item[8] if item[0] == 'flir' else item[4]


def frame_bytes(item):
    if item[0] == 'flir':
        return item[5].nbytes + 1024
    return sum(getattr(array, 'nbytes', 0) for array in item[1][1:3]) + 4096


class FrameBuffer:
    """Reserve bytes until disk work finishes, including dispatcher/staging queues.

    Controls do not consume the frame budget. Their FIFO ordering is preserved;
    a full frame buffer can never prevent Stop/barrier/shutdown from being queued.
    Each selected stream has an equal share so one stalled sink cannot starve the
    others. Frame puts never block an acquisition callback.
    """

    def __init__(self, limit_bytes=FRAME_BUFFER_BYTES):
        self.limit_bytes = limit_bytes
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.streams = {}
        self.pending_bytes = 0
        self.pending_frames = 0

    def configure_streams(self, names):
        with self.lock:
            if self.pending_frames:
                raise RuntimeError('The previous camera frames have not finished saving.')
            self.streams = {name: 0 for name in names}

    def reserve(self, item):
        name, size = frame_stream(item), frame_bytes(item)
        with self.lock:
            allowance = self.limit_bytes // max(1, len(self.streams))
            if self.pending_bytes + size > self.limit_bytes or self.streams.get(name, 0) + size > allowance:
                raise queue.Full
            self.streams[name] = self.streams.get(name, 0) + size
            self.pending_bytes += size
            self.pending_frames += 1

    def release(self, item):
        name, size = frame_stream(item), frame_bytes(item)
        with self.lock:
            self.streams[name] -= size
            self.pending_bytes -= size
            self.pending_frames -= 1

    def put_reserved(self, item):
        self.queue.put_nowait(item)

    def put(self, item, block=True, timeout=None):
        if item[0] in ('seek', 'flir'):
            self.reserve(item)
        self.put_reserved(item)

    put_nowait = put

    def get(self, block=True, timeout=None):
        return self.queue.get(block=block, timeout=timeout)

    def snapshot(self):
        with self.lock:
            return dict(bytes=self.pending_bytes, frames=self.pending_frames,
                        limit_bytes=self.limit_bytes, stream_bytes=dict(self.streams))


class RecordingJournal:
    """One background owner for an append-only event file and atomic checkpoints."""

    def __init__(self, root, take_id):
        self.path = Path(root) / f'take{take_id:02d}_events.jsonl'
        self.report_path = Path(root) / f'take{take_id:02d}_sync.json'
        self.queue = queue.Queue(maxsize=4096)
        self.lock = threading.Lock()
        self.pending_report = None
        self.closing = threading.Event()
        self.dropped_events = 0
        self.error = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='Recording journal')
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            raise OSError(self.error or 'Recording journal did not start.')

    def event(self, event):
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            with self.lock:
                self.dropped_events += 1

    def checkpoint(self, report):
        # Only the newest snapshot is needed; a slow disk cannot accumulate copies.
        with self.lock:
            self.pending_report = report

    def close(self):
        if self.thread.is_alive():
            self.closing.set()
            self.thread.join(10)
        if self.thread.is_alive() or self.error:
            raise OSError(self.error or 'Recording journal did not finish.')

    def _run(self):
        try:
            # A new take must never truncate an older journal.
            with self.path.open('x', encoding='utf-8') as handle:
                self.ready.set()
                last_flush = time.monotonic()
                dirty = False
                reported_drops = 0
                while True:
                    try:
                        value = self.queue.get(timeout=.25)
                    except queue.Empty:
                        value = None
                    if value is not None:
                        handle.write(json.dumps(value, ensure_ascii=False) + '\n')
                        dirty = True
                    with self.lock:
                        report, self.pending_report = self.pending_report, None
                        dropped = self.dropped_events
                    if dropped != reported_drops:
                        handle.write(json.dumps(dict(command='DIAGNOSTIC_BACKPRESSURE',
                            dropped_events=dropped, host_monotonic_ns=time.perf_counter_ns())) + '\n')
                        reported_drops = dropped
                        dirty = True
                    if report is not None:
                        report['diagnostic_events_dropped'] = dropped
                        handle.flush()
                        os.fsync(handle.fileno())
                        dirty = False
                        atomic_json(self.report_path, report)
                    with self.lock:
                        finished = self.closing.is_set() and self.queue.empty() and self.pending_report is None
                    if finished:
                        handle.flush()
                        os.fsync(handle.fileno())
                        return
                    if dirty and time.monotonic() - last_flush >= 1:
                        handle.flush()
                        os.fsync(handle.fileno())
                        last_flush = time.monotonic()
                        dirty = False
        except Exception as exc:
            self.error = f'Recording journal: {exc}'
            log.exception('Recording journal failed')
        finally:
            self.ready.set()


class PowerInhibitor:
    """Set and clear execution state on the same owning watchdog thread."""

    def __init__(self, setter=None):
        self.setter = setter
        if setter is None and sys.platform == 'win32':
            self.setter = ctypes.windll.kernel32.SetThreadExecutionState
            self.setter.argtypes = [ctypes.c_uint]
            self.setter.restype = ctypes.c_uint
        self.active = False

    def set_active(self, active):
        if bool(active) == self.active:
            return
        if self.setter and not self.setter(0x80000001 if active else 0x80000000):
            raise OSError('Windows rejected the recording sleep-prevention request.')
        self.active = bool(active)


def configure_recording_logging(folder=None):
    """Called by launchers, never by imports or tests; callbacks log asynchronously."""
    global _listener, _crash_file
    if _listener is not None:
        return
    root = Path(folder) if folder else Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'UltramicStudio' / 'logs'
    root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(root / 'recording.log', maxBytes=5 * 1024 ** 2,
                                  backupCount=4, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(threadName)s %(message)s'))
    pending = queue.Queue(maxsize=4096)
    log.addHandler(QueueHandler(pending))
    log.setLevel(logging.INFO)
    log.propagate = False
    _listener = QueueListener(pending, handler)
    _listener.start()
    def stop_logging():
        # QueueListener.stop uses a nonblocking sentinel, which can fail on a full
        # bounded queue. Allow its listener to drain before inserting the sentinel.
        _listener.queue.put(None, timeout=5)
        _listener._thread.join(5)
        handler.close()
    atexit.register(stop_logging)
    crash_path = root / 'native-crash.log'
    if crash_path.exists() and crash_path.stat().st_size > 5 * 1024 ** 2:
        crash_path.replace(root / 'native-crash.previous.log')
    _crash_file = crash_path.open('a', encoding='utf-8')
    faulthandler.enable(_crash_file)
    old_sys_hook, old_thread_hook = sys.excepthook, threading.excepthook

    def exception_hook(exc_type, value, trace):
        log.critical('Unhandled application exception', exc_info=(exc_type, value, trace))
        old_sys_hook(exc_type, value, trace)

    def thread_hook(args):
        log.critical('Unhandled worker exception', exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        old_thread_hook(args)

    sys.excepthook, threading.excepthook = exception_hook, thread_hook
    log.info('Recorder started: Python %s; diagnostics %s', sys.version, root)
