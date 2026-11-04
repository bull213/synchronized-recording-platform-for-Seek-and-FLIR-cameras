import os
import runpy
import time
import wave
from pathlib import Path

import cv2
import numpy as np
import pytest

from camera_demo import DemoCameraCore
from recording_coordinator import RecordingCoordinator
from ultramic_studio.audio import DemoSource
from ultramic_studio.core import RecordingConfig
from ultramic_studio.export import export_mp3
from ultramic_studio.synchronized import SynchronizedAudioEngine


def wait_for(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.01)
    assert check()


def shutdown(controller):
    controller.shutdown()
    wait_for(lambda: any(e[0] == 'shutdown_complete' for e in controller.core.ui_events.queue))


@pytest.mark.parametrize('flir_connected', [False, True])
@pytest.mark.parametrize('sample_mode', ['all', 'custom', 'after_shutter'])
def test_seek_only_records_without_flir_output(tmp_path, monkeypatch, flir_connected, sample_mode):
    core = DemoCameraCore()
    if not flir_connected:
        monkeypatch.setattr(core, 'start_blackfly', lambda: (None, None, []))
    controller = RecordingCoordinator(core)
    controller.configure(output=str(tmp_path), recording_mode='seek_only',
                         sample_mode=sample_mode, sample_hz=10)
    if sample_mode == 'after_shutter':
        controller.shutter_settings.update(automatic=False, hz=10)
    assert not controller.ready()
    controller.connect()
    try:
        wait_for(lambda: not controller.connecting)
        assert controller.ready()
        assert controller._selected_streams() == ['seek']
        assert not controller.options['microphone']
        for _ in range(2):
            assert controller.request('START')['ok']
            wait_for(lambda: controller.take['frame_counts'].get('seek', 0) >= 4)
            assert controller.request('STOP')['ok']
            assert controller.finished.wait(8)
            assert controller.take['status'] == 'complete', controller.take
            assert set(controller.take['written_frame_counts']) == {'seek'}
        videos = list(controller.run_dir.rglob('*.mp4'))
        assert videos
        assert not list(controller.run_dir.rglob('*flir*'))
        assert list(controller.run_dir.rglob('*thermal*.csv'))
        for path in videos:
            video = cv2.VideoCapture(str(path))
            try:
                assert video.read()[0]
            finally:
                video.release()
    finally:
        shutdown(controller)


def test_flir_only_without_seek_saves_only_playable_mp4s(tmp_path, monkeypatch):
    core = DemoCameraCore()
    def unavailable():
        raise RuntimeError('Seek is not installed')
    monkeypatch.setattr(core, 'start_seek', unavailable)
    controller = RecordingCoordinator(core)
    controller.configure(output=str(tmp_path), recording_mode='flir_mp4')
    controller.connect()
    try:
        wait_for(lambda: not controller.connecting)
        assert not controller.cameras_ready
        assert controller.ready()
        assert controller.request('ARM')['ok']
        for _ in range(2):
            assert controller.request('START', source='tcp')['ok']
            wait_for(lambda: controller.take['frame_counts'].get('flir_cam1', 0) >= 4)
            controller.request('STOP', source='tcp')
            assert controller.finished.wait(8)
            assert controller.take['status'] == 'complete', controller.take
        files = [p for p in controller.run_dir.rglob('*') if p.is_file()]
        assert len(files) == 4
        assert {p.suffix for p in files} == {'.mp4'}
        for path in files:
            video = cv2.VideoCapture(str(path))
            try:
                assert video.read()[0]
            finally:
                video.release()
    finally:
        shutdown(controller)


@pytest.mark.parametrize('fail_export', [False, True])
def test_microphone_only_without_cameras_and_export_recovery(tmp_path, monkeypatch, fail_export):
    import recording_coordinator
    core = DemoCameraCore()
    controller = RecordingCoordinator(core)
    controller.configure(output=str(tmp_path), recording_mode='microphone_mp3')
    engine = SynchronizedAudioEngine(RecordingConfig(output=tmp_path, demo=True))
    source = DemoSource(engine)
    controller.audio = engine
    source.start()
    if fail_export:
        def broken(*args, **kwargs):
            raise OSError('Encoder failed')
        monkeypatch.setattr(recording_coordinator, 'export_mp3', broken)
    try:
        wait_for(controller.ready)
        assert controller.request('ARM')['ok']
        assert controller.request('START', source='tcp')['ok']
        wait_for(lambda: engine.snapshot()['recorded_samples'] > 60_000)
        assert controller.request('STOP', source='tcp')['ok']
        assert controller.finished.wait(8)
        assert controller.writer is None
        files = [p for p in controller.run_dir.rglob('*') if p.is_file()]
        if fail_export:
            assert controller.take['status'] == 'error'
            assert any('WAV retained' in e for e in controller.take['errors'])
            assert any(p.suffix == '.wav' for p in files)
        else:
            assert controller.take['status'] == 'complete', controller.take
            assert len(files) == 1 and files[0].suffix == '.mp3'
            assert files[0].stat().st_size > 1000
            assert not list(controller.run_dir.glob('.audio_*'))
    finally:
        shutdown(controller)
        source.stop()
        engine.close()


def test_normal_speed_mp3_duration_and_no_sidecar(tmp_path):
    wav = tmp_path / 'input.wav'
    with wave.open(str(wav), 'wb') as handle:
        handle.setparams((1, 2, 200_000, 0, 'NONE', 'not compressed'))
        tone = (np.sin(np.arange(200_000) * 2 * np.pi * 1000 / 200_000) * 12000).astype('<i2')
        handle.writeframes(tone.tobytes())
    output = export_mp3([wav], tmp_path / 'normal.mp3', time_expansion=1, write_metadata=False)
    # Count MPEG-1 layer III frames in the real encoded output, including padding.
    data = output.read_bytes()
    offset = frames = 0
    rates = (44100, 48000, 32000)
    bitrates = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    while offset + 4 <= len(data):
        header = int.from_bytes(data[offset:offset + 4], 'big')
        assert header >> 21 == 0x7ff
        assert (header >> 19) & 3 == 3
        assert (header >> 17) & 3 == 1
        rate = rates[(header >> 10) & 3]
        bitrate = bitrates[(header >> 12) & 15] * 1000
        offset += 144 * bitrate // rate + ((header >> 9) & 1)
        frames += 1
    assert offset == len(data)
    assert 1 <= frames * 1152 / rate < 1.15
    assert not output.with_suffix('.mp3.json').exists()


def test_camera_module_import_does_not_require_camera_sdks(monkeypatch):
    import camera_runtime
    def fail():
        raise AssertionError('Seek SDK must load only when connecting')
    monkeypatch.setattr(camera_runtime, 'find_seek_dll', fail)
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'sync-rec.py'))
    assert namespace['seek'] is None
    assert namespace['PySpin'] is None


def test_gui_modes_and_draggable_preview_panels(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtCore, QtTest, QtWidgets
    from ultramic_studio import gui
    from unified_gui import UnifiedWindow
    monkeypatch.setattr(gui, 'list_devices', lambda: [])
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = UnifiedWindow(DemoCameraCore())
    window.output.setText(str(tmp_path))
    window.resize(1420, 950)
    window.show()
    def pump(check):
        def tick():
            app.processEvents()
            return check()
        wait_for(tick)
    try:
        pump(lambda: len(window._preview_labels) == 3 and
             window._preview_rows and window._preview_rows[0].count() >= 2)
        row = window._preview_rows[0]
        before = row.sizes()
        handle = row.handle(1)
        point = handle.rect().center()
        QtTest.QTest.mousePress(handle, QtCore.Qt.MouseButton.LeftButton, pos=point)
        QtTest.QTest.mouseMove(handle, point + QtCore.QPoint(60, 0), delay=20)
        QtTest.QTest.mouseRelease(handle, QtCore.Qt.MouseButton.LeftButton,
                                 pos=point + QtCore.QPoint(60, 0))
        app.processEvents()
        assert row.sizes() != before
        window.record_mode.setCurrentIndex(4)
        assert window.record_mode.currentText() == 'Seek only'
        assert window.controller.options['recording_mode'] == 'seek_only'
        assert window.record.isEnabled()
        assert window.sample_mode.isEnabled()
        assert window.calibration.isEnabled()
        window.record_mode.setCurrentIndex(2)
        assert window.controller.options['recording_mode'] == 'flir_mp4'
        assert window.record.isEnabled()
        window.command('START')
        pump(lambda: window.controller.take['frame_counts'].get('flir_cam1', 0) > 2)
        window.command('STOP')
        pump(lambda: window.controller.state == 'idle')
        flir_folder = window.controller.run_dir
        window.record_mode.setCurrentIndex(3)
        assert not window.record.isEnabled()
        window.mode.setCurrentIndex(1)
        window.toggle_connection()
        pump(lambda: window.record.isEnabled())
        window.command('START')
        pump(lambda: window.engine.snapshot()['recorded_samples'] > 40_000)
        window.command('STOP')
        pump(lambda: window._last_mp3 is not None)
        assert window._last_mp3.exists()
        assert not window.exports
        assert window.controller.run_dir != flir_folder
        assert {p.suffix for p in flir_folder.iterdir()} == {'.mp4'}
        assert {p.suffix for p in window.controller.run_dir.iterdir()} == {'.mp3'}
    finally:
        window.close()
        pump(lambda: window._shutdown_complete)
