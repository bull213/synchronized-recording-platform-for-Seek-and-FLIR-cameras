"""Render documentation screenshots with synthetic inputs and isolated preferences."""
from pathlib import Path
import os
import sys
import tempfile
import time
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def main():
    import numpy as np
    from PySide6 import QtCore, QtGui, QtWidgets
    from ultramic_studio import gui
    from camera_demo import DemoCameraCore
    from unified_gui import UnifiedWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    if os.name == 'nt':
        fonts = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts'
        for name in ('segoeui.ttf', 'consola.ttf'):
            QtGui.QFontDatabase.addApplicationFont(str(fonts / name))

    def wait_for(check, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if check():
                return
            time.sleep(.02)
        raise RuntimeError('Synthetic GUI did not become ready.')

    def capture(window, relative):
        window.output.setText('recordings')
        window.serial_number.clear()
        window.log_box.setPlainText('Synthetic demonstration. No physical devices are open.')
        app.processEvents()
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(path)), f'Could not save {relative}'
        print(relative)

    with tempfile.TemporaryDirectory(prefix='Ultramic_demo_') as temporary, \
            patch.object(gui, 'list_devices', return_value=[]), \
            patch('serial.tools.list_ports.comports', return_value=[]):
        temp = Path(temporary)
        QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
        for scope in (QtCore.QSettings.Scope.UserScope, QtCore.QSettings.Scope.SystemScope):
            QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, scope, str(temp / 'settings'))
        window = gui.MainWindow()
        try:
            window.output.setText(str(temp / 'recordings'))
            window.session_label.setText('demo_session')
            window.auto_mp3.setChecked(False)
            window.mode.setCurrentIndex(1)
            window.show()
            window.toggle_connection()
            wait_for(lambda: window.engine is not None and window.engine.snapshot()['samples'] >= 650_000)
            capture(window, 'docs/live-preview.png')

            samples = np.arange(600_000) / 200_000
            signal = (.3 * np.sin(2 * np.pi * 60_000 * samples) * np.hanning(len(samples)) * 32767)
            recording = temp / 'synthetic_demo.wav'
            with wave.open(str(recording), 'wb') as wav:
                wav.setparams((1, 2, 200_000, 0, 'NONE', 'not compressed'))
                wav.writeframes(signal.astype('<i2').tobytes())
            window.set_review_file(recording)
            wait_for(lambda: window.review_future is None)
            capture(window, 'docs/review-preview.png')
        finally:
            window.close()
            app.processEvents()
            window.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

        combined = UnifiedWindow(DemoCameraCore())
        try:
            combined.output.setText(str(temp / 'recordings'))
            combined.session_label.setText('demo_session')
            combined.auto_mp3.setChecked(False)
            combined.mode.setCurrentIndex(1)
            combined.record_mode.setCurrentIndex(1)
            combined.show()
            combined.resize(1420, 950)
            combined.toggle_connection()
            wait_for(lambda: combined.controller.cameras_ready and combined.engine is not None
                     and combined.engine.snapshot()['samples'] >= 650_000)
            combined.tabs.setCurrentIndex(0)
            capture(combined, 'docs/images/unified_cameras.png')
            combined.tabs.setCurrentIndex(1)
            capture(combined, 'docs/images/unified_spectrogram.png')
        finally:
            combined.close()
            wait_for(lambda: combined._shutdown_complete, timeout=30)
            combined.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    main()
