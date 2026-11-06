"""Explicitly labelled synthetic cameras for testing without camera SDKs or USB."""
from __future__ import annotations

import queue
import threading
import time

import cv2
import numpy as np

from ultramic_studio.recorder.storage import write_camera_files


class _Camera:
    def EndAcquisition(self):
        pass

    def DeInit(self):
        pass


class DemoCameraCore:
    demo = True
    SEEKCAMERA_SHUTTER_MODE_AUTO = 0
    SEEKCAMERA_SHUTTER_MODE_MANUAL = 1

    def __init__(self):
        self.coordinator = None
        self.recording = threading.Event()
        self.stop_all = threading.Event()
        self.seek_connected = threading.Event()
        self.first_seek_frame = threading.Event()
        self.latest_ir_lock = threading.Lock()
        self.flir_preview_lock = threading.Lock()
        self.latest_ir = None
        self.flir_previews = {}
        self.save_q = queue.Queue(maxsize=1024)
        self.ui_events = queue.Queue()
        self.active_seek_cam = None
        self.seek_sample_mode = 'custom'
        self.seek_sample_hz = 1.0
        self.seek_worker = None
        self.seek_worker_stop = threading.Event()

    def start_seek(self, generation=0, cancel=None):
        self.first_seek_frame.clear()
        self.seek_connected.set()
        self.seek_worker_stop = threading.Event()
        self.seek_worker = threading.Thread(target=self._seek, args=(generation, self.seek_worker_stop),
                                            daemon=True, name='DEMO Seek')
        self.seek_worker.start()
        if not self.first_seek_frame.wait(2):
            raise RuntimeError('Synthetic Seek failed to start.')
        return None

    def start_blackfly(self):
        return None, None, [dict(index=i, serial=f'DEMO_FLIR_{i}', cam=_Camera(), acquiring=True)
                            for i in range(2)]

    def _seek(self, generation, stop):
        frame = 0
        yy, xx = np.mgrid[:60, :80]
        while not self.stop_all.is_set() and not stop.is_set():
            received = time.perf_counter_ns()
            raw = (25 + 8 * np.exp(-((xx - (frame % 70)) ** 2 + (yy - 30) ** 2) / 120)).astype(np.float32)
            color = cv2.applyColorMap(((raw - 25) * 30).astype(np.uint8), cv2.COLORMAP_INFERNO)
            bgra = cv2.cvtColor(color, cv2.COLOR_BGR2BGRA)
            snap = (time.time(), bgra, raw, dict(timestamp_utc_ns=time.time_ns(),
                                                 fpa_frame_count=frame, width=80, height=60,
                                                 type=16, channels=1, pixel_depth=32,
                                                 units='C', source='SYNTHETIC DEMO'), received)
            with self.latest_ir_lock:
                self.latest_ir = snap
            if self.coordinator:
                self.coordinator.on_seek(snap, generation=generation)
            self.first_seek_frame.set()
            frame += 1
            self.stop_all.wait(1 / 27)

    def flir_thread(self, index, camera, generation=0, stop=None):
        frame = 0
        yy, xx = np.mgrid[:120, :160]
        while not self.stop_all.is_set() and not (stop and stop.is_set()):
            received = time.perf_counter_ns()
            data = ((xx + yy + frame * 2 + index * 30) % 256).astype(np.uint8)
            preview = cv2.cvtColor(data, cv2.COLOR_GRAY2BGR)
            with self.flir_preview_lock:
                self.flir_previews[index] = preview
            if self.coordinator:
                self.coordinator.on_flir(index, frame, frame * 33_333_333,
                                         time.time(), data, received, generation=generation)
            frame += 1
            self.stop_all.wait(1 / 30)

    def shutter_thread(self):
        while not self.stop_all.is_set():
            if self.coordinator:
                self.coordinator.shutter_tick()
            self.stop_all.wait(.01)

    def trigger_seek_shutter(self):
        if not self.seek_connected.is_set():
            raise RuntimeError('Synthetic Seek is not connected.')
        return 0

    def configure_seek_shutter(self, *args):
        pass

    def recover_seek(self, owner, generation, cancel):
        self.seek_worker_stop.set()
        if self.seek_worker:
            self.seek_worker.join(2)
            if self.seek_worker.is_alive():
                raise RuntimeError('Synthetic Seek did not stop.')
        if not cancel.is_set():
            self.start_seek(generation, cancel)
        if cancel.is_set():
            self.seek_worker_stop.set()
            self.seek_connected.clear()
            owner.seek_ready = owner.cameras_ready = False

    def recover_flir(self, record):
        record.update(cam=_Camera(), acquiring=True)

    def writer_thread(self, run_dir, cam_indices, ready_event=None):
        write_camera_files(self, run_dir, cam_indices, ready_event)
