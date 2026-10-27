import os
import time
import weakref
from types import SimpleNamespace

import pytest

from camera_demo import DemoCameraCore
from recording_coordinator import RecordingCoordinator


def test_shutdown_releases_camera_references_before_sdk_clear():
    core = DemoCameraCore()
    controller = RecordingCoordinator(core)
    calls = []

    class Camera:
        def EndAcquisition(self):
            calls.append('end')

        def DeInit(self):
            calls.append('deinit')

    camera = Camera()
    handle = weakref.ref(camera)
    controller.flir_cams = [dict(cam=camera, acquiring=True)]
    # The connection event shares the dictionaries; it must not retain handles.
    core.ui_events.put(('connected', controller.flir_cams))
    del camera

    def clear():
        assert handle() is None, 'Camera handle still held when clearing SDK'
        calls.append('clear')

    controller.cam_list = SimpleNamespace(Clear=clear)
    controller.system = SimpleNamespace(ReleaseInstance=lambda: calls.append('release'))
    controller._shutdown_worker()
    assert calls == ['end', 'deinit', 'clear', 'release']
    assert controller.cam_list is None and controller.system is None
    assert ('shutdown_complete',) in list(core.ui_events.queue)


@pytest.mark.parametrize('failing_step', ['EndAcquisition', 'DeInit', 'Clear', 'ReleaseInstance'])
def test_sdk_cleanup_error_still_releases_seek_and_finishes_shutdown(failing_step):
    core = DemoCameraCore()
    controller = RecordingCoordinator(core)
    calls = []

    def action(name):
        def run(*args):
            calls.append(name)
            if name == failing_step:
                raise RuntimeError('Simulated SDK cleanup failure')
        return run

    controller.flir_cams = [dict(cam=SimpleNamespace(
        EndAcquisition=action('EndAcquisition'), DeInit=action('DeInit')), acquiring=True)]
    controller.cam_list = SimpleNamespace(Clear=action('Clear'))
    controller.system = SimpleNamespace(ReleaseInstance=action('ReleaseInstance'))
    core.active_seek_cam = object()
    core.seek = SimpleNamespace(seekcamera_capture_session_stop=action('seek_stop'),
                                seekcamera_manager_destroy=action('seek_destroy'))
    core.ctypes = SimpleNamespace(byref=lambda value: value)
    controller.manager = object()
    controller._shutdown_worker()
    assert calls == ['EndAcquisition', 'DeInit', 'Clear', 'ReleaseInstance', 'seek_stop', 'seek_destroy']
    assert core.active_seek_cam is None and controller.manager is None
    events = list(core.ui_events.queue)
    assert any(event[0] == 'message' and 'cleanup error' in event[1] for event in events)
    assert events[-1] == ('shutdown_complete',)


def test_window_closes_after_sdk_release_error(monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets
    from ultramic_studio import gui
    from unified_gui import UnifiedWindow

    monkeypatch.setattr(gui, 'list_devices', lambda: [])
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = UnifiedWindow(DemoCameraCore())
    window.show()

    def pump(check):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            app.processEvents()
            if check():
                return
            time.sleep(.01)
        assert check()

    def release():
        raise RuntimeError('Simulated Spinnaker retained reference')

    try:
        pump(lambda: window.controller.cameras_ready)
        window.controller.system = SimpleNamespace(ReleaseInstance=release)
        window.close()
        pump(lambda: window._shutdown_complete and not window.isVisible())
        assert not window.timer.isActive()
    finally:
        window.close()
        pump(lambda: window._shutdown_complete)
