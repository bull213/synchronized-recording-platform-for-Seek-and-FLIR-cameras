import json
import socket
import time

from camera_demo import DemoCameraCore
from recording_coordinator import RecordingCoordinator
from ultramic_studio.audio import DemoSource
from ultramic_studio.core import RecordingConfig
from ultramic_studio.synchronized import SynchronizedAudioEngine
from ultramic_studio.triggers import SoftwareTrigger


def wait_for(check, timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(.01)
    assert check()


def send(port, command):
    with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
        sock.sendall((json.dumps(command) + '\n').encode())
        return json.loads(sock.makefile('rb').readline())


def test_camera_only_tcp_commands_share_the_seek_gate(tmp_path):
    core = DemoCameraCore()
    controller = RecordingCoordinator(core)
    controller.configure(output=str(tmp_path), microphone=False)
    controller.connect()
    server = SoftwareTrigger(lambda: controller, 0)
    try:
        wait_for(lambda: controller.cameras_ready)
        assert not send(server.port, {'command': 'START'})['ok']
        assert controller.request('ARM')['ok']
        first = send(server.port, {'command': 'START', 'label': 'trigger_trial'})
        duplicate = send(server.port, {'command': 'START'})
        assert first['ok'] and duplicate['already_recording']
        wait_for(lambda: all(controller.take['frame_counts'].get(name) for name in ('seek', 'flir_cam0', 'flir_cam1')))
        assert send(server.port, {'command': 'MARK', 'label': 'event'})['ok']
        assert send(server.port, {'command': 'STOP'})['ok']
        assert controller.finished.wait(5)
        report = json.loads((controller.run_dir / 'take01_sync.json').read_text())
        assert report['status'] == 'complete'
        assert report['label'] == 'trigger_trial'
        assert report['audio_status'] == 'disabled'
        assert any(event['command'] == 'MARK' and event['label'] == 'event' for event in report['events'])
        assert send(server.port, {'command': 'STOP'})['ok']
        assert controller.snapshot()['armed']
    finally:
        server.close()
        controller.shutdown()
        wait_for(lambda: core.stop_all.is_set())


def test_audio_failure_drains_camera_files_and_allows_camera_only_next_take(tmp_path):
    core = DemoCameraCore()
    controller = RecordingCoordinator(core)
    audio = SynchronizedAudioEngine(RecordingConfig(output=tmp_path, demo=True, auto_mp3=False))
    source = DemoSource(audio)
    controller.audio = audio
    controller.configure(output=str(tmp_path), microphone=True)
    source.start()
    controller.connect()
    try:
        wait_for(lambda: controller.cameras_ready and audio.snapshot()['samples'] > 10_000)
        assert controller.request('START')['ok']
        wait_for(lambda: audio.snapshot()['recorded_samples'] > 20_000)
        audio.fault('Simulated microphone disconnect')
        wait_for(lambda: audio.snapshot()['error'] is not None)
        controller.fault(audio.snapshot()['error'])
        assert controller.finished.wait(5)
        assert controller.take['status'] == 'error'
        assert controller.take['audio_status'] == 'error'
        first_parts = [part for part in controller.take['segments'].values() if part['stream'] == 'flir_cam0']
        assert first_parts and all(part['status'] == 'complete' for part in first_parts)
        assert all((controller.run_dir / part['file']).stat().st_size > 0 for part in first_parts)
        controller.configure(microphone=False)
        assert controller.request('START')['ok']
        wait_for(lambda: controller.take['frame_counts'].get('flir_cam1', 0) > 2)
        controller.request('STOP')
        assert controller.finished.wait(5)
        assert controller.take['status'] == 'complete'
        second_parts = [part for part in controller.take['segments'].values() if part['stream'] == 'flir_cam0']
        assert second_parts and all(part['take_id'] == 2 for part in second_parts)
        assert all((controller.run_dir / part['file']).is_file() for part in second_parts)
        assert {part['file'] for part in first_parts}.isdisjoint(part['file'] for part in second_parts)
        assert not (controller.run_dir / 'microphone' / 'take02').exists()
    finally:
        controller.shutdown()
        wait_for(lambda: core.stop_all.is_set())
        source.stop()
        audio.close()


def test_bad_output_folder_marks_take_error_without_hanging(tmp_path):
    controller = RecordingCoordinator(DemoCameraCore())
    controller.cameras_ready = True
    controller.configure(output=str(tmp_path / 'missing'))
    assert controller.request('START')['ok']
    assert controller.finished.wait(5)
    assert controller.take['status'] == 'error'
    assert controller.state == 'idle'
    controller.shutdown()

