"""Portable app diagnostics and a short recording check that needs no hardware."""
from __future__ import annotations

import importlib
import json
import platform
import runpy
import struct
import tempfile
import time
import wave
from pathlib import Path

from camera_runtime import application_folder, resource_folder


def _report(lines, path):
    text = '\n'.join(lines) + '\n'
    print(text)
    if path:
        Path(path).write_text(text, encoding='utf-8')


def check_setup(report=None):
    lines = ['Seek / FLIR / Ultramic - setup check', f'Windows/platform: {platform.platform()}',
             f'Architecture: {struct.calcsize("P") * 8}-bit', f'App folder: {application_folder()}']
    failed = platform.system() != 'Windows' or struct.calcsize('P') != 8
    for name in ('numpy', 'cv2', 'PySide6.QtWidgets', 'pyqtgraph', 'sounddevice', 'serial', 'lameenc'):
        try:
            importlib.import_module(name)
            lines.append(f'OK: {name}')
        except Exception as exc:
            failed = True
            lines.append(f'FAILED: {name}: {exc}')
    try:
        core = runpy.run_path(str(resource_folder() / 'sync-rec.py'), run_name='camera_setup_check')
        # The capture core now loads vendor SDKs lazily when cameras connect.
        # Diagnostics must explicitly load them without starting acquisition.
        core['_load_seek']()
        lines.append(f'OK: Seek SDK library: {core["_load_seek"].__globals__["SEEK_DLL_PATH"]}')
        spin = importlib.import_module('PySpin')
        system = spin.System.GetInstance()
        try:
            cameras = system.GetCameras()
            try:
                count = cameras.GetSize()
                lines.append(f'OK: FLIR SDK; {count} connected camera(s) detected')
                if not count:
                    lines.append('CHECK: Connect/power the FLIRs. If they remain undetected, run Install_Camera_Drivers.cmd as administrator.')
            finally:
                cameras.Clear()
        finally:
            system.ReleaseInstance()
        lines.append('CHECK: Verify Seek streaming in the app after connecting it; this check does not start capture.')
    except Exception as exc:
        failed = True
        lines.append(f'FAILED: Camera SDK loading: {exc}')
    try:
        from ultramic_studio.audio import list_devices
        devices = [d for d in list_devices() if 'ultra' in d['name'].lower() or 'dodotronic' in d['name'].lower()]
        for device in devices:
            lines.append(f'Microphone available: {device["name"]} [{device["host_api"]}]')
        if not devices:
            lines.append('Optional microphone not detected. Cameras only mode remains available.')
        lines.append('Microphone enumeration does not verify 200 kHz capture; connect/monitor in the app to test it.')
    except Exception as exc:
        lines.append(f'Optional microphone device list unavailable: {exc}. Cameras only mode remains available.')
    lines.append('SETUP CHECK: ' + ('FAILED - see errors above' if failed else 'PASSED - software libraries loaded'))
    _report(lines, report)
    return int(failed)


def self_test(report=None):
    lines = ['Seek / FLIR / Ultramic - synthetic recording self-test']
    controller = engine = source = None
    temporary = None
    result = 1
    try:
        import os
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6 import QtWidgets
        from camera_demo import DemoCameraCore
        from recording_coordinator import RecordingCoordinator
        from ultramic_studio.audio import DemoSource
        from ultramic_studio.core import RecordingConfig
        from ultramic_studio.export import export_session
        from ultramic_studio.synchronized import SynchronizedAudioEngine
        import cv2

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        widget = QtWidgets.QLabel('Recorder self-test')
        widget.resize(200, 50)
        assert not widget.grab().isNull(), 'Qt rendering failed'
        temporary = tempfile.TemporaryDirectory(prefix='SyncRecorder_test_')
        root = Path(temporary.name)
        engine = SynchronizedAudioEngine(RecordingConfig(output=root, demo=True, pre_seconds=.1, auto_mp3=False))
        source = DemoSource(engine)
        controller = RecordingCoordinator(DemoCameraCore())
        controller.audio = engine
        controller.configure(output=str(root), label='SELF_TEST', microphone=True, sample_mode='all')
        source.start()
        controller.connect()

        def wait_for(check, seconds=10):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                app.processEvents()
                if check():
                    return
                time.sleep(.01)
            raise RuntimeError('Timed out waiting for recording self-test')

        wait_for(lambda: controller.cameras_ready and engine.snapshot()['samples'] >= 50_000)
        assert controller.request('START')['ok']
        wait_for(lambda: engine.snapshot()['recorded_samples'] >= 40_000)
        assert controller.request('STOP')['ok']
        wait_for(lambda: controller.finished.is_set())
        assert controller.take['status'] == 'complete', controller.take['errors']
        session = controller.run_dir / controller.take['audio_path']
        with wave.open(str(session / 'audio_0001.wav'), 'rb') as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (200_000, 1, 2)
            assert wav.getnframes() > 0
            lines.append(f'OK: Native-format synthetic WAV: {wav.getnframes()} samples')
        assert (session / 'pretrigger.wav').exists()
        report_path = controller.run_dir / 'take01_sync.json'
        saved = json.loads(report_path.read_text(encoding='utf-8'))
        segments = list(saved['segments'].values())
        for stream in ('seek', 'flir_cam0', 'flir_cam1'):
            parts = [part for part in segments if part['stream'] == stream]
            assert parts, f'{stream} has no saved video parts'
            for part in parts:
                assert part['status'] == 'complete' and part['frames'] > 0
                capture = cv2.VideoCapture(str(controller.run_dir / part['file']))
                try:
                    assert capture.read()[0], f'{stream} video part could not be decoded'
                    assert round(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == part['frames']
                finally:
                    capture.release()
                assert (controller.run_dir / part['timing_csv']).is_file()
        assert (controller.run_dir / 'seek_thermal.csv').stat().st_size > 0
        mp3 = export_session(session)
        assert mp3.exists() and mp3.stat().st_size > 0
        lines.extend(['OK: Qt rendering, segmented Seek and two FLIR MP4s, thermal CSV, separate pre-trigger, timing report, MP3 export',
                      'SELF-TEST: PASSED (synthetic data; hardware was not opened)'])
        result = 0
    except Exception as exc:
        lines.append(f'SELF-TEST: FAILED: {exc}')
    finally:
        if controller:
            controller.shutdown()
            controller.connect_done.wait(20)
            controller.finished.wait(10)
            if controller.writer:
                controller.writer.join(timeout=10)
            # Wait for SDK/demo shutdown before removing its output files.
            deadline = time.monotonic() + 10
            while not controller.core.stop_all.is_set() and time.monotonic() < deadline:
                time.sleep(.01)
        if source:
            source.stop()
        if engine:
            engine.close()
        if temporary:
            temporary.cleanup()
    _report(lines, report)
    return result
