"""Laptop geometry and view changes exercised with live synthetic cameras."""
import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from camera_demo import DemoCameraCore
from unified_gui import UnifiedWindow


def pump(app, check=lambda: True, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if check():
            return
        time.sleep(.02)
    assert check()


@pytest.fixture
def window(tmp_path, monkeypatch, example_calibration):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # Windows' offscreen plugin does not discover system fonts itself.
    if os.name == 'nt':
        QtGui.QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
        QtGui.QFontDatabase.addApplicationFont('C:/Windows/Fonts/consola.ttf')
    original = QtCore.QSettings
    monkeypatch.setattr(QtCore, 'QSettings', lambda *args: original(
        str(tmp_path / 'display.ini'), original.Format.IniFormat))
    win = UnifiedWindow(DemoCameraCore())
    win.output.setText(str(tmp_path))
    win.show()
    pump(app, lambda: len(win._preview_labels) == 3 and win.controller.cameras_ready)
    yield app, win
    win.close()
    pump(app, lambda: win._shutdown_complete, seconds=25)
    app.processEvents()


@pytest.mark.parametrize('physical_width,physical_height,scale', [
    (1366, 768, 1), (1366, 768, 1.25), (1366, 768, 1.5),
    (1920, 1080, 1), (1920, 1080, 1.25), (1920, 1080, 1.5),
])
def test_laptop_controls_fit_and_previews_expand(window, physical_width, physical_height, scale):
    app, win = window
    width = int(physical_width / scale) - 24
    height = int((physical_height - 48) / scale) - 48
    win.resize(width, height)
    pump(app)
    assert win.width() <= width
    assert win.height() <= height
    for widget in (win.record, win.stop, win.settings_button, win.preview_button, win.record_mode):
        point = widget.mapTo(win, QtCore.QPoint(0, 0))
        assert win.rect().contains(QtCore.QRect(point, widget.size()))
        assert widget.isVisible()
    before = win.preview_scroll.viewport().width()
    win.settings_button.click()
    pump(app)
    assert not win.settings_scroll.isVisible()
    assert win.preview_scroll.viewport().width() > before
    win.preview_button.click()
    pump(app, lambda: win._preview_rows[0].count() == 3)
    assert not win.camera_options.isVisible()
    assert not win.log_box.isVisible()
    assert win.stop.isVisible() and win.elapsed.isVisible()
    assert win._preview_rows[0].count() == 3
    win._enlarge_buttons['flir_0'].click()
    pump(app)
    assert win._camera_panels['flir_0'].isVisible()
    assert not win._camera_panels['seek'].isVisible()
    QtTest.QTest.keyClick(win, QtCore.Qt.Key.Key_Escape)
    pump(app)
    assert all(panel.isVisible() for panel in win._camera_panels.values())
    QtTest.QTest.keyClick(win, QtCore.Qt.Key.Key_Escape)
    pump(app)
    assert not win._preview_mode
    win._reset_layout()
    pump(app)
    assert win.settings_scroll.isVisible()
    assert win.camera_options.isVisible()
    assert not win.log_box.isVisible()
    for index in range(win.tabs.count()):
        win.tabs.setCurrentIndex(index)
        pump(app)
        assert win.width() <= width and win.height() <= height


def test_layout_changes_during_recording_and_preferences(window):
    app, win = window
    win.command('START')
    pump(app, lambda: win.controller.take is not None and
         win.controller.take['shared_start_monotonic_ns'] is not None)
    win._toggle_settings()
    win.options_action.setChecked(False)
    win.log_action.setChecked(True)
    win._toggle_preview_mode()
    win._enlarge_camera('seek')
    pump(app, lambda: win._preview_labels['seek'].pixmap() is not None)
    pixmap = win._preview_labels['seek'].pixmap()
    assert not pixmap.isNull()
    assert win.controller.is_recording
    pump(app, lambda: time.perf_counter_ns() - win.controller.take['shared_start_monotonic_ns'] > 400_000_000)
    win.command('STOP')
    pump(app, lambda: win.controller.finished.is_set())
    assert win.controller.take['status'] == 'complete', win.controller.take['errors']
    assert win.settings.value('layout/settings', type=bool) is False
    assert win.settings.value('layout/options', type=bool) is False
    assert win.settings.value('layout/log', type=bool) is True
    win._escape_view()
    win._escape_view()
    pump(app)
    assert win.log_box.isVisible()
    assert not win.settings_scroll.isVisible()


def test_double_click_aspect_ratio_and_restore_preferences(window):
    app, win = window
    win.resize(1200, 720)
    win.body_splitter.setSizes([350, 850])
    win._save_sidebar_width()
    win._toggle_settings()
    win.options_action.setChecked(False)
    win.log_action.setChecked(True)
    preview = win._preview_labels['seek']
    QtTest.QTest.mouseDClick(preview, QtCore.Qt.MouseButton.LeftButton)
    pump(app, lambda: win._focused_camera == 'seek' and preview.pixmap() is not None)
    with win.core.latest_ir_lock:
        frame = win.core.latest_ir[1]
    pixmap = preview.pixmap()
    assert abs(pixmap.width() / pixmap.height() - frame.shape[1] / frame.shape[0]) < .04
    win.close()
    pump(app, lambda: win._shutdown_complete, seconds=25)
    restored = UnifiedWindow(DemoCameraCore())
    restored.show()
    try:
        pump(app, lambda: restored.controller.cameras_ready)
        assert restored._focused_camera is None
        assert not restored._preview_mode
        assert not restored.settings_scroll.isVisible()
        assert not restored.camera_options.isVisible()
        assert restored.log_box.isVisible()
        restored._toggle_settings()
        restored.core.ui_events.put(('connect_error', 'Synthetic connection failure'))
        pump(app, lambda: restored.camera_status.text() == 'Camera connection failed')
        restored._toggle_preview_mode()
        pump(app)
        assert restored.camera_status.isVisible()
        assert restored.stop.isVisible()
    finally:
        restored.close()
        pump(app, lambda: restored._shutdown_complete, seconds=25)


def test_calibration_selector_is_locked_for_armed_and_active_takes(window):
    app, win = window
    assert win.calibration.currentData() == 'none'
    win.calibration.setCurrentIndex(win.calibration.findData('example-calibration'))
    assert win.controller.options['seek_calibration'] == 'example-calibration'
    assert win.settings.value('seek/calibration') == 'example-calibration'
    win.record_mode.setCurrentIndex(2)
    assert not win.calibration.isEnabled()
    win.record_mode.setCurrentIndex(0)
    assert win.calibration.isEnabled()
    win.toggle_arm(True)
    pump(app, lambda: not win.calibration.isEnabled())
    assert win.controller.armed
    win.command('START')
    pump(app, lambda: win.controller.state == 'recording')
    assert not win.calibration.isEnabled()
    assert win.controller.take['seek_calibration']['name'] == 'example-calibration'
    pump(app, lambda: all(win.controller.take['frame_counts'].get(name, 0)
                         for name in ('seek', 'flir_cam0', 'flir_cam1')))
    win.command('STOP')
    assert not win.calibration.isEnabled()
    pump(app, lambda: win.controller.finished.is_set())
    assert win.controller.take['status'] == 'complete', win.controller.take['errors']
    win.toggle_arm(False)
    pump(app, lambda: win.calibration.isEnabled())
    win.calibration.setCurrentIndex(win.calibration.findData('none'))
    assert win.controller.options['seek_calibration'] == 'none'


@pytest.mark.parametrize('saved_name,expected_name', [
    ('example-calibration', 'example-calibration'), ('removed-room', 'none'),
])
def test_calibration_preference_restored_with_unknown_room_fallback(window, saved_name, expected_name):
    app, win = window
    win.calibration.setCurrentIndex(win.calibration.findData('example-calibration'))
    if saved_name == 'removed-room':
        win.settings.setValue('seek/calibration', saved_name)
    win.close()
    pump(app, lambda: win._shutdown_complete, seconds=25)
    restored = UnifiedWindow(DemoCameraCore())
    try:
        assert restored.calibration.currentData() == expected_name
        assert restored.controller.options['seek_calibration'] == expected_name
    finally:
        restored.close()
        pump(app, lambda: restored._shutdown_complete, seconds=25)
