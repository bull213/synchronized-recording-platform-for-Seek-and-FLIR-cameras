"""Capture the actual recorder using synthetic inputs and isolated settings.

Run from the source root; only writes under .validation/recorder-redesign.
Use QT_SCALE_FACTOR=1.25 or 1.5 in a separate process to inspect Windows scaling.
"""
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6 import QtCore, QtGui, QtWidgets
from camera_demo import DemoCameraCore
from unified_gui import UnifiedWindow


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle('Fusion')
    if os.name == 'nt':
        for name in ('segoeui.ttf', 'consola.ttf'):
            QtGui.QFontDatabase.addApplicationFont(str(Path(os.environ['SystemRoot']) / 'Fonts' / name))
    output = ROOT / '.validation' / 'recorder-redesign'
    output.mkdir(parents=True, exist_ok=True)
    scale = float(os.environ.get('QT_SCALE_FACTOR', '1'))

    def wait(check, timeout=25):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if check():
                return
            time.sleep(.02)
        raise AssertionError('Synthetic recorder timed out')

    def capture(win, name, width, height):
        recording_output = win.output.text()
        win.output.setText('recordings / demo_session')
        win.output.setCursorPosition(0)
        win.resize(int(width / scale), int(height / scale))
        for _ in range(8):
            app.processEvents()
            time.sleep(.03)
        if name == 'seek_settings':
            win.settings_scroll.ensureWidgetVisible(win.shutter_mode, 12, 12)
            app.processEvents()
        for widget in (win.record, win.stop, win.elapsed, win.readiness_label):
            position = widget.mapTo(win, QtCore.QPoint())
            assert win.rect().contains(QtCore.QRect(position, widget.size()))
        path = output / f'{name}_{scale:g}x.png'
        assert win.grab().save(str(path))
        win.output.setText(recording_output)
        print(path)

    with tempfile.TemporaryDirectory() as temporary, patch('ultramic_studio.gui.list_devices', return_value=[]), patch('serial.tools.list_ports.comports', return_value=[]):
        original = QtCore.QSettings
        with patch.object(QtCore, 'QSettings', lambda *args: original(str(Path(temporary) / 'preferences.ini'), original.Format.IniFormat)):
            win = UnifiedWindow(DemoCameraCore())
            win.output.setText(temporary)
            win.session_label.setText('demo_session')
            win.show()
            try:
                wait(lambda: win.controller.cameras_ready and win._preview_labels['seek'].pixmap() is not None)
                capture(win, 'ready_desktop', 1420, 900)
                win.seek_section.toggle.setChecked(True)
                win.settings_scroll.ensureWidgetVisible(win.shutter_mode)
                capture(win, 'seek_settings', 1420, 900)
                win.seek_section.toggle.setChecked(False)
                win.settings_scroll.verticalScrollBar().setValue(0)
                win.record_mode.setCurrentIndex(1)
                win.mode.setCurrentIndex(1)
                win.auto_mp3.setChecked(False)
                win.toggle_connection()
                wait(lambda: win.engine is not None and win.record.isEnabled())
                win.audio_toggle.setChecked(True)
                win.command('START')
                wait(lambda: win.controller.state == 'recording' and win.engine.snapshot()['recorded_samples'] > 150000)
                capture(win, 'recording_audio', 1420, 900)
                win.audio_toggle.setChecked(False)
                capture(win, 'recording_laptop', 1320, 690)
                win.core.ui_events.put(('writer_warning', 'Demonstration warning: check disk space.'))
                wait(lambda: 'disk space' in win.readiness_label.text())
                capture(win, 'warning_laptop', 1320, 690)
                win.command('STOP')
                wait(lambda: win.controller.finished.is_set() and 'Saved' in win.result_label.text())
                win.dismiss_notice.click()
                capture(win, 'saved_desktop', 1420, 900)
            finally:
                win.close()
                wait(lambda: win._shutdown_complete)


if __name__ == '__main__':
    main()
