import csv
import json
import os
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from camera_demo import DemoCameraCore
from recording_coordinator import RecordingCoordinator
from ultramic_studio.core import AudioBlock, RecordingConfig
from ultramic_studio.synchronized import SynchronizedAudioEngine


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.01)
    assert check(), 'Condition did not complete.'


def read_audio(folder):
    meta = json.loads((folder / 'session.json').read_text())
    pcm = bytearray()
    for segment in meta['segments']:
        with wave.open(str(folder / segment['file']), 'rb') as f:
            assert (f.getframerate(), f.getnchannels(), f.getsampwidth()) == (200_000, 1, 2)
            assert f.getnframes() == segment['samples']
            pcm.extend(f.readframes(f.getnframes()))
    return meta, bytes(pcm)


def feed(engine, data, first, end):
    adc = 1 + first / 200_000
    assert engine.feed(AudioBlock(data[first:end].tobytes(), first, adc, adc + .02,
                                 round((adc + .02) * 1e9)))


def test_in_block_start_and_retroactive_stop_keep_exact_pcm(tmp_path):
    engine = SynchronizedAudioEngine(RecordingConfig(output=tmp_path, pre_seconds=.005, auto_mp3=False))
    engine.set_clock_mapping(0, 500, 1)
    data = np.arange(10_000, dtype=np.int16)
    folder = tmp_path / 'take01'
    try:
        feed(engine, data, 0, 2048)
        engine.request('ARM')
        assert engine.start_at(1, 1_002_502_000, folder).result(2)['ok']
        feed(engine, data, 2048, 4096)
        feed(engine, data, 4096, 8000)
        engine.request('ARM')
        assert engine.stop_at(1, 1_024_997_000).result(2)['ok']
        meta, pcm = read_audio(folder)
        assert pcm == data[501:5000].tobytes()
        assert meta['status'] == 'complete'
        assert meta['first_saved_monotonic_ns'] == 1_002_505_000
        assert meta['start_offset_ns'] == 3000
        assert meta['separate_pretrigger_samples'] == 501
        with wave.open(str(folder / 'pretrigger.wav'), 'rb') as f:
            assert f.readframes(f.getnframes()) == data[:501].tobytes()
        rows = list(csv.DictReader((folder / 'audio_timing.csv').open()))
        assert sum(int(row['frames']) for row in rows) == 4499
    finally:
        engine.close()


def test_pending_audio_boundaries_and_segments_are_contiguous(tmp_path):
    engine = SynchronizedAudioEngine(RecordingConfig(output=tmp_path, pre_seconds=0,
                                                    segment_seconds=1, auto_mp3=False))
    engine.set_clock_mapping(0, 200, 1)
    data = np.arange(450_000, dtype=np.int32).astype(np.int16)
    folder = tmp_path / 'take01'
    try:
        assert engine.start_at(1, 1_000_252_000, folder).result(2)['ok']
        assert engine.snapshot()['pending']
        assert engine.stop_at(1, 3_100_002_000).result(2)['ok']
        for first in range(0, len(data), 2048):
            feed(engine, data, first, min(first + 2048, len(data)))
        engine.request('ARM')
        meta, pcm = read_audio(folder)
        assert pcm == data[51:420001].tobytes()
        assert len(meta['segments']) == 3
        assert not (folder / 'pretrigger.wav').exists()
    finally:
        engine.close()


def test_expired_audio_history_fails_instead_of_using_later_sample(tmp_path):
    engine = SynchronizedAudioEngine(RecordingConfig(output=tmp_path, auto_mp3=False))
    engine.set_clock_mapping(0, 200, 2)
    assert engine.feed(AudioBlock(b'\x00\x00' * 1000, 0, 2, 2.01))
    engine.request('ARM')
    result = engine.start_at(1, 1_000_000_000, tmp_path / 'take01').result(2)
    assert not result['ok']
    wait_for(lambda: not engine.thread.is_alive())
    assert 'history' in engine.snapshot()['error']
    engine.close()


def test_invalid_adc_and_batched_callbacks_keep_a_contiguous_sample_timeline(tmp_path):
    engine = SynchronizedAudioEngine(RecordingConfig(output=tmp_path, pre_seconds=0, auto_mp3=False,
                                                    device={'driver_reported_input_latency_seconds': .02}))
    engine.set_clock_mapping(0, 200, 1)
    data = np.arange(4096, dtype=np.int16)
    folder = tmp_path / 'take01'
    try:
        # ADC lies in the future; both callbacks have the same driver time.
        assert engine.feed(AudioBlock(data[:2048].tobytes(), 0, 1.1, 1.02))
        assert engine.feed(AudioBlock(data[2048:].tobytes(), 2048, 1.2, 1.02))
        engine.request('ARM')
        assert engine.start_at(1, 1_012_500_000, folder).result(2)['ok']
        assert engine.stop_at(1, 1_017_500_000).result(2)['ok']
        meta, pcm = read_audio(folder)
        assert pcm == data[2500:3500].tobytes()
        assert meta['timestamp_source'] == 'driver_latency_estimate'
        assert meta['start_offset_ns'] == 0
    finally:
        engine.close()


def snap(host_ns):
    return (time.time(), np.zeros((60, 80, 4), dtype=np.uint8),
            np.full((60, 80), 25, dtype=np.float32),
            {'timestamp_utc_ns': time.time_ns(), 'fpa_frame_count': 1}, host_ns)


@pytest.mark.parametrize('mode', ['custom', 'all'])
def test_camera_only_gate_rejects_cached_frames_and_logs_offsets(tmp_path, mode):
    core = DemoCameraCore()
    controller = RecordingCoordinator(core)
    controller.cameras_ready = True
    controller.flir_cams = [dict(index=0, serial='DEMO', cam=None)]
    controller.configure(output=str(tmp_path), sample_mode=mode, microphone=False)
    try:
        assert controller.request('START')['ok']
        request_ns = controller.take['request_monotonic_ns']
        controller.on_seek(snap(request_ns - 1))
        controller.on_flir(0, 1, 100, time.time(), np.zeros((60, 80), dtype=np.uint8), request_ns)
        assert controller.take['shared_start_monotonic_ns'] is None
        first_ns = time.perf_counter_ns()
        controller.on_seek(snap(first_ns))
        controller.on_flir(0, 2, 200, time.time(), np.zeros((60, 80), dtype=np.uint8), first_ns - 1)
        controller.on_flir(0, 3, 300, time.time(), np.zeros((60, 80), dtype=np.uint8), first_ns + 1000)
        assert controller.request('STOP')['ok']
        assert controller.finished.wait(5)
        report = json.loads((controller.run_dir / 'take01_sync.json').read_text())
        assert report['status'] == 'complete'
        assert report['audio_status'] == 'disabled'
        assert report['first_streams']['seek']['offset_from_seek_ns'] == 0
        assert report['first_streams']['flir_cam0']['offset_from_seek_ns'] == 1000
        assert report['frame_counts'] == {'seek': 1, 'flir_cam0': 1}
        assert not (controller.run_dir / 'microphone').exists()
        controller.configure(microphone=True)
        assert not controller.request('START')['ok']
        controller.configure(microphone=False)
        assert controller.request('START')['ok']
        controller.request('STOP')
        assert controller.finished.wait(5)
        assert controller.take['status'] == 'cancelled'
    finally:
        controller.flir_cams = []
        controller.shutdown()
        wait_for(lambda: core.stop_all.is_set())


def test_first_post_click_seek_frame_is_retained_during_gui_settings_read(tmp_path):
    core = DemoCameraCore()
    controller = RecordingCoordinator(core)
    controller.cameras_ready = True
    controller.flir_cams = [dict(index=0, serial='DEMO', cam=None)]
    controller.on_flir(0, 0, 0, time.time(), np.zeros((60, 80), dtype=np.uint8), time.perf_counter_ns())
    controller.configure(output=str(tmp_path))
    click_ns = time.perf_counter_ns()
    # These callbacks precede dispatch of START, but follow the button click.
    controller.on_seek(snap(click_ns - 1))
    controller.on_seek(snap(click_ns + 1))
    controller.on_seek(snap(click_ns + 2))
    try:
        assert controller.request('START', request_monotonic_ns=click_ns)['ok']
        assert controller.take['shared_start_monotonic_ns'] == click_ns + 1
        assert controller.take['first_streams']['seek']['offset_from_seek_ns'] == 0
        controller.on_flir(0, 1, 1, time.time(), np.zeros((60, 80), dtype=np.uint8), time.perf_counter_ns())
        controller.request('STOP')
        assert controller.finished.wait(5)
    finally:
        controller.shutdown()
        wait_for(lambda: core.stop_all.is_set())


@pytest.mark.parametrize('after_shutter', [False, True])
def test_unified_gui_camera_only_and_synchronized_audio_workflow(tmp_path, monkeypatch, after_shutter):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets
    from ultramic_studio import gui
    from unified_gui import UnifiedWindow
    monkeypatch.setattr(gui, 'list_devices', lambda: [])
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = UnifiedWindow(DemoCameraCore())
    errors = []
    monkeypatch.setattr(window, 'error', errors.append)
    window.output.setText(str(tmp_path))
    window.auto_mp3.setChecked(True)
    window.show()

    def pump(check, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            application.processEvents()
            if check():
                return
            time.sleep(.01)
        assert check(), errors

    try:
        pump(lambda: window.controller.cameras_ready)
        if after_shutter:
            window.sample_mode.setCurrentIndex(2)
            window.shutter_mode.setCurrentIndex(1)
            window.shutter_hz.setValue(10)
            window._apply_shutter()
        assert window.record_mode.currentIndex() == 0
        pump(lambda: window.record.isEnabled())
        window.command('START')
        pump(lambda: window.controller.state == 'recording')
        pump(lambda: all(window.controller.take['frame_counts'].get(f'flir_cam{i}', 0) > 0
                         for i in (0, 1)))
        window.command('STOP')
        pump(lambda: window.controller.state == 'idle')
        assert window.controller.take['status'] == 'complete'
        assert not (window.controller.run_dir / 'microphone').exists()

        window.mode.setCurrentIndex(1)
        window.toggle_connection()
        pump(lambda: window.engine is not None and window.engine.snapshot()['samples'] > 10_000)
        window.record_mode.setCurrentIndex(1)
        window.live.set_frequency_range(50, 100)
        window.pause.setChecked(True)
        window.command('START')
        pump(lambda: window.engine.snapshot()['recorded_samples'] > 30_000)
        window.command('STOP')
        pump(lambda: window.controller.state == 'idle')
        pump(lambda: window.last_session is not None and not window.exports)
        assert window.controller.take['status'] == 'complete', window.controller.take
        assert (window.last_session / 'listening_10x.mp3').exists()
        assert (window.last_session / 'pretrigger.wav').exists()
        assert window.live.frequency_range == (50, 100)
        window.set_review_file(window.last_session / 'audio_0001.wav')
        pump(lambda: window.review_future is None)
        assert window.tabs.currentIndex() == 2
        assert window.review.image.image is not None
        assert not errors

        # External commands can finish several takes while Qt is busy. Every
        # completion must retain its own report/path and automatic MP3 export.
        folders = []
        for number in (3, 4):
            assert window.controller.request('START')['ok']
            wait_for(lambda: window.engine.snapshot().get('session_path', '').endswith(f'take{number:02d}')
                     and window.engine.snapshot()['recorded_samples'] > 25_000)
            assert window.controller.request('STOP')['ok']
            assert window.controller.finished.wait(5)
            assert window.controller.take['status'] == 'complete'
            folders.append(window.controller.run_dir / window.controller.take['audio_path'])
        pump(lambda: all((folder / 'listening_10x.mp3').exists() for folder in folders) and not window.exports)
        assert 'take04_sync.json' in window.sync_summary.text()
    finally:
        window.close()
        pump(lambda: window._shutdown_complete, 15)
