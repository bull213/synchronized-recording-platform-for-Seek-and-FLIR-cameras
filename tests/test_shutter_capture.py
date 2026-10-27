import json
import os
import threading
import time

import numpy as np
import pytest

from camera_demo import DemoCameraCore
from camera_runtime import SeekShutterFrameGate
from recording_coordinator import RecordingCoordinator


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.01)
    assert check()


def frame(received):
    return (time.time(), np.zeros((60, 80, 4), dtype=np.uint8),
            np.ones((60, 80), dtype=np.float32), {'fpa_frame_count': 1}, received)


def test_gate_requires_success_return_and_valid_fresh_frame():
    clock = [100]
    gate = SeekShutterFrameGate(lambda: clock[0])
    token = gate.begin(1)
    assert gate.begin(1) is None
    assert gate.consume(101, 1) is None
    def trigger():
        assert gate.consume(101, 1) is None  # Reentrant SDK callback cannot deadlock.
        clock[0] = 110
        return 0
    request = gate.request(token, trigger)
    assert request['shutter_return_monotonic_ns'] == 110
    assert gate.consume(110, 1) is None
    assert gate.consume(111, 2) is None
    assert gate.consume(111, 1, valid=False) is None
    selected = gate.consume(112, 1)
    assert selected['shutter_request_id'] == token
    assert selected['shutter_frame_received_monotonic_ns'] == 112
    assert gate.consume(113, 1) is None


def test_cancel_during_sdk_call_cannot_publish_into_new_take():
    gate = SeekShutterFrameGate()
    entered, release = threading.Event(), threading.Event()
    results = []
    token = gate.begin(1)
    def trigger():
        entered.set()
        assert release.wait(2)
        return 0
    worker = threading.Thread(target=lambda: results.append(gate.request(token, trigger)))
    worker.start()
    assert entered.wait(2)
    gate.cancel()
    new_token = gate.begin(2)
    release.set()
    worker.join(2)
    assert results == [None]
    assert gate.consume(time.perf_counter_ns(), 2) is None
    gate.request(new_token, lambda: 0)
    assert gate.consume(time.perf_counter_ns(), 2)['take_id'] == 2


def test_gate_error_and_timeout():
    clock = [0]
    gate = SeekShutterFrameGate(lambda: clock[0])
    with pytest.raises(RuntimeError, match='error 7'):
        gate.request(gate.begin(1), lambda: 7)
    assert gate.pending is None
    gate.request(gate.begin(1), lambda: 0)
    clock[0] = 5_000_000_000
    assert gate.expired()
    gate.cancel()
    assert not gate.expired()


def controller_for_test(tmp_path):
    core = DemoCameraCore()
    core.seek_connected.set()
    controller = RecordingCoordinator(core)
    controller.cameras_ready = True
    controller.configure(output=str(tmp_path), sample_mode='after_shutter')
    controller.apply_shutter('Custom', 1)
    return core, controller


def close(controller):
    controller.request('STOP')
    assert controller.finished.wait(5)
    controller.shutdown()
    wait_for(lambda: controller.core.stop_all.is_set())
    if controller.writer:
        controller.writer.join(5)


def test_selected_frame_sets_shared_start_and_saves_metadata(tmp_path):
    core, controller = controller_for_test(tmp_path)
    controller.flir_cams = [dict(index=0, serial='DEMO', cam=None)]
    try:
        assert controller.request('START')['ok']
        controller.on_seek(frame(time.perf_counter_ns()))
        assert controller.take['shared_start_monotonic_ns'] is None
        controller.shutter_tick()
        request = dict(controller.shutter_gate.pending)
        # A scheduled tick while waiting must not replace the pending request.
        controller.next_shutter_ns = 0
        controller.shutter_tick()
        assert controller.shutter_gate.pending == request
        invalid = list(frame(time.perf_counter_ns()))
        invalid[2] = None
        controller.on_seek(tuple(invalid))
        assert controller.take['shared_start_monotonic_ns'] is None
        selected_ns = time.perf_counter_ns()
        controller.on_seek(frame(selected_ns))
        controller.on_seek(frame(selected_ns + 1))
        controller.on_flir(0, 1, 20, time.time(), np.zeros((60, 80), dtype=np.uint8), selected_ns)
        assert controller.take['frame_counts']['seek'] == 1
        assert controller.take['shared_start_monotonic_ns'] == selected_ns
        controller.request('STOP')
        assert controller.finished.wait(5)
        report = json.loads((controller.run_dir / 'take01_sync.json').read_text())
        assert report['status'] == 'complete'
        event = next(e for e in report['events'] if e['command'] == 'SHUTTER_FRAME')
        assert event['shutter_return_monotonic_ns'] < selected_ns
        assert len(list((controller.run_dir / 'video_parts').glob('seek_*.mp4'))) == 1
        assert not list(controller.run_dir.rglob('*.png'))
        assert 'shutter_timing=after_successful_request' in (controller.run_dir / 'seek_thermal.csv').read_text()
        assert controller.request('START')['ok']
        controller.on_seek(frame(selected_ns))
        assert controller.take['shared_start_monotonic_ns'] is None
    finally:
        close(controller)


@pytest.mark.parametrize('failure', ['sdk', 'timeout', 'disconnect'])
def test_capture_failure_requests_recovery_and_preserves_other_streams(tmp_path, monkeypatch, failure):
    core, controller = controller_for_test(tmp_path)
    # Drive the watchdog deterministically; no background recovery or hardware.
    monkeypatch.setattr(controller, '_ensure_monitor', lambda: None)
    controller.flir_cams = [dict(index=0, serial='DEMO', cam=None)]
    recovered = threading.Event()

    def recover(stream, generation, take_id, cancel):
        assert stream == 'seek' and take_id == controller.take_number
        with controller.lock:
            controller._recovery_pending.discard(stream)
        recovered.set()

    monkeypatch.setattr(controller, '_recover_stream', recover)
    try:
        assert controller.request('START')['ok']
        controller.shutter_tick()
        controller.on_seek(frame(time.perf_counter_ns()))
        controller.on_flir(0, 1, 1, time.time(), np.zeros((60, 80), dtype=np.uint8), time.perf_counter_ns())
        assert controller.take['shared_start_monotonic_ns'] is not None
        controller.next_shutter_ns = 0
        if failure == 'sdk':
            core.trigger_seek_shutter = lambda: 9
        controller.shutter_tick()
        if failure == 'timeout':
            controller.shutter_gate.clock = lambda: time.perf_counter_ns() + 6_000_000_000
            controller.shutter_tick()
        elif failure == 'disconnect':
            controller.seek_disconnected()
            controller.on_seek(frame(time.perf_counter_ns()), generation=0)
        assert controller.is_recording and not controller.finished.is_set()
        assert any(event['command'] == 'CAMERA_WARNING' for event in controller.take['events'])
        now = time.perf_counter_ns() + controller.STALE_NS + 1
        controller._health('flir_cam0')['last_received_ns'] = now
        controller._monitor_once(now)
        assert recovered.wait(2)
        assert controller.take['recovery_counts']['seek'] == 1
        assert controller.shutter_gate.pending is None
        controller.on_flir(0, 2, 2, time.time(), np.zeros((60, 80), dtype=np.uint8), now + 1)
        assert controller.take['frame_counts']['flir_cam0'] == 2
        assert controller.request('STOP', request_monotonic_ns=now + 2)['ok']
        assert controller.finished.wait(5)
        assert controller.take['status'] == 'complete_with_gaps', controller.take['errors']
        assert controller.take['gaps'] and controller.take['has_gaps']
    finally:
        close(controller)


def test_demo_gui_after_shutter_recording(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets
    from ultramic_studio import gui
    from unified_gui import UnifiedWindow
    monkeypatch.setattr(gui, 'list_devices', lambda: [])
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = UnifiedWindow(DemoCameraCore())
    def pump(check):
        def tick():
            app.processEvents()
            return check()
        wait_for(tick)
    try:
        window.output.setText(str(tmp_path))
        pump(lambda: window.controller.cameras_ready)
        window.sample_mode.setCurrentIndex(2)
        assert not window.sample_hz.isEnabled()
        window._sync_options()
        assert not window.controller.request('START')['ok']  # Automatic rejected.
        window.shutter_mode.setCurrentIndex(1)
        window.shutter_hz.setValue(10)
        window._apply_shutter()
        window.command('START')
        pump(lambda: window.controller.take['frame_counts'].get('seek', 0) >= 3)
        window.update_controls()
        assert not window.apply_shutter_button.isEnabled()
        window.command('STOP')
        pump(lambda: window.controller.state == 'idle')
        assert window.controller.take['status'] == 'complete'
        events = window.controller.take['events']
        selected = [e for e in events if e['command'] == 'SHUTTER_FRAME']
        assert len({e['shutter_request_id'] for e in selected}) == len(selected)
        assert all(e['shutter_frame_received_monotonic_ns'] > e['shutter_return_monotonic_ns']
                   for e in selected)
    finally:
        window.close()
        pump(lambda: window._shutdown_complete)
