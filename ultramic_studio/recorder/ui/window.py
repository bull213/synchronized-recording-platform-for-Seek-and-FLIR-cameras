"""One Qt event loop for camera previews, microphone monitoring and review."""
from __future__ import annotations

import queue
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets as W

from ultramic_studio.theme import RECORDER_STYLE
from ultramic_studio.recorder.ui import camera_previews
from ultramic_studio.recorder.ui.presentation import RecorderPresentation
from ultramic_studio.recorder.coordinator import RecordingCoordinator
from ultramic_studio.recorder.cameras.calibration import SEEK_CALIBRATIONS, get_seek_calibration
from ultramic_studio.audio import DemoSource, create_input_source
from ultramic_studio.core import RecordingConfig
from ultramic_studio.gui import MainWindow, button, label
from ultramic_studio.synchronized import SynchronizedAudioEngine, calibrate_source_clock
from ultramic_studio.triggers import SerialTrigger, SoftwareTrigger


class UnifiedWindow(RecorderPresentation, MainWindow):
    def __init__(self, core, *, demo_audio=False):
        self.core = core
        self.controller = RecordingCoordinator(core)
        self._closing = self._cameras_closed = self._shutdown_complete = False
        self._mic_future = self._mic_action = self._audio_saved = None
        self._mic_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='Mic connection')
        self._next_clock_check = 0
        self._preview_labels = {}
        self._device_labels = {}
        self._preview_rows = []
        self._camera_panels = {}
        self._enlarge_buttons = {}
        self._focused_camera = None
        self._layout_signature = None
        self._preview_mode = False
        self._last_mp3 = None
        self._roi_window = None
        self._roi_close_pending = False
        super().__init__()
        self.setWindowTitle('Seek / FLIR / Ultramic Recorder')
        self.setStyleSheet(RECORDER_STYLE)
        self.setMinimumSize(560, 320)
        self._fit_screen()
        QtCore.QTimer.singleShot(0, self._restore_sidebar_width)
        self.statusBar().showMessage('Camera-only recording is available without a microphone.')
        self.controller.connect()
        if demo_audio:
            self.mode.setCurrentIndex(1)
            self.record_mode.setCurrentIndex(1)
            self.toggle_connection()

    def _build(self):
        self.settings = QtCore.QSettings('UltramicStudio', 'CombinedRecorder')
        super()._build()
        if not Path(self.output.text()).expanduser().is_dir():
            self.output.setText(str(Path.cwd()))
        self.findChild(W.QLabel, 'title').setText('Seek / FLIR / Ultramic')
        # Keep shared Record/Stop reachable when the sidebar needs scrolling.
        root = self.centralWidget()
        self.header_widget.hide()
        self.record_mode = W.QComboBox()
        self.record_mode.setAccessibleName('Recording mode')
        self.record_mode.addItems(['Cameras only', 'Cameras + microphone',
                                   'FLIR only (.mp4)', 'Microphone only (.mp3)', 'Seek only'])
        self.record_mode.currentIndexChanged.connect(self._mode_changed)
        source_group = self.source_group
        source_group.setTitle('01   OPTIONAL MICROPHONE')
        self.pre.setToolTip('Saved separately as pretrigger.wav. The main WAV begins at the Seek start.')
        self.stop.setToolTip('Stop the shared take; armed external triggers remain active.')

        cameras = W.QWidget()
        self.monitor_page = cameras
        layout = W.QVBoxLayout(cameras)
        row = W.QHBoxLayout()
        self.camera_status = label('Connecting cameras…')
        self.camera_status.setWordWrap(True)
        row.addWidget(self.camera_status, 1)
        self.retry_cameras = button('Retry cameras', self.controller.connect)
        row.addWidget(self.retry_cameras)
        layout.addLayout(row)
        options = W.QGroupBox('Seek camera options')
        grid = W.QGridLayout(options)
        self.sample_mode = W.QComboBox()
        self.sample_mode.addItems(['Custom rate', 'Every available frame', 'After shutter request'])
        self.sample_mode.setToolTip('Selected Seek frames are saved as MP4 video with temperature and timing CSVs. '
                                    'After shutter request saves one fresh frame per successful custom shutter call; '
                                    'physical shutter completion is not confirmed.')
        self.sample_hz = W.QDoubleSpinBox()
        self.sample_hz.setRange(.01, 1000)
        self.sample_hz.setValue(1)
        self.sample_hz.setSuffix(' Hz')
        grid.addWidget(label('Sampling'), 0, 0)
        grid.addWidget(self.sample_mode, 0, 1)
        grid.addWidget(self.sample_hz, 0, 2)
        self.sample_mode.currentIndexChanged.connect(self.update_controls)
        self.shutter_mode = W.QComboBox()
        self.shutter_mode.addItems(['Automatic', 'Custom'])
        self.shutter_hz = W.QDoubleSpinBox()
        self.shutter_hz.setRange(.01, 27)
        self.shutter_hz.setValue(1)
        self.shutter_hz.setSuffix(' Hz')
        grid.addWidget(label('Shutter'), 1, 0)
        grid.addWidget(self.shutter_mode, 1, 1)
        grid.addWidget(self.shutter_hz, 1, 2)
        self.apply_shutter_button = button('Apply shutter', self._apply_shutter)
        grid.addWidget(self.apply_shutter_button, 1, 3)
        self.calibration = W.QComboBox()
        for profile in SEEK_CALIBRATIONS.values():
            self.calibration.addItem(profile.label, profile.name)
        saved_calibration = self.settings.value('seek/calibration', 'none')
        self.calibration.setCurrentIndex(max(0, self.calibration.findData(saved_calibration)))
        self.calibration.currentIndexChanged.connect(self._calibration_changed)
        self._calibration_changed()
        grid.addWidget(label('Calibration used'), 2, 0)
        grid.addWidget(self.calibration, 2, 1, 1, 3)
        self.preview_actions = []
        for column, (text, action) in enumerate((('Cycle palette', 'palette'),
                                               ('Cycle pipeline', 'pipeline'), ('Toggle sharpening', 'sharpen'))):
            control = button(text, lambda checked=False, a=action: self._camera_action(a))
            self.preview_actions.append(control)
            grid.addWidget(control, 3, column)
        self.camera_options = W.QScrollArea()
        self.camera_options.setWidgetResizable(True)
        self.camera_options.setWidget(options)
        self.camera_options.setFixedHeight(200)
        layout.addWidget(self.camera_options)
        self.preview_widget = W.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.preview_widget.setChildrenCollapsible(False)
        self.preview_widget.setHandleWidth(8)
        self.preview_widget.setToolTip('Drag the dividers to resize camera previews.')
        self.preview_scroll = W.QScrollArea()
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setWidget(self.preview_widget)
        self.preview_scroll.setMinimumSize(0, 0)
        layout.addWidget(self.preview_scroll, 1)
        self._reflow_timer = QtCore.QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._reflow_previews)
        self.preview_scroll.viewport().installEventFilter(self)
        self._add_camera_panel('seek', 'Seek camera', 0)
        self.sync_summary = label('Each take saves actual first-stream offsets in its synchronization report.', True)
        self.sync_summary.setWordWrap(True)
        layout.addWidget(self.sync_summary)
        self.tabs.insertTab(0, cameras, 'Cameras')
        self.tabs.setCurrentIndex(0)
        trigger_page = W.QWidget()
        trigger_layout = W.QVBoxLayout(trigger_page)
        group = self.trigger_group
        group.parentWidget().layout().removeWidget(group)
        trigger_layout.addWidget(group)
        note = label('Armed commands control the selected recording mode.', True)
        note.setWordWrap(True)
        trigger_layout.addWidget(note)
        trigger_layout.addStretch()
        self.tabs.addTab(trigger_page, 'Triggers')
        self._build_layout_controls(root)
        self._build_recorder_presentation()
        self.update_controls()

    def _add_camera_panel(self, key, title, index):
        return camera_previews.add_camera_panel(self, key, title, index)

    def _build_layout_controls(self, root):
        root.layout().setContentsMargins(8, 6, 8, 6)
        controls = W.QWidget()
        rows = W.QVBoxLayout(controls)
        rows.setContentsMargins(0, 0, 0, 0)
        transport = W.QHBoxLayout()
        for control in (self.record, self.stop, self.elapsed):
            transport.addWidget(control)
        duration_layout = self.metrics_widget.layout().takeAt(0).layout()
        while duration_layout.count():
            item = duration_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        duration_layout.deleteLater()
        self.badge.setWordWrap(True)
        self.badge.setMinimumWidth(0)
        self.badge.setSizePolicy(W.QSizePolicy.Policy.Maximum, W.QSizePolicy.Policy.Preferred)
        transport.addWidget(self.badge)
        transport.addStretch(1)
        rows.addLayout(transport)
        views = W.QHBoxLayout()
        self.record_mode.setMinimumContentsLength(10)
        self.record_mode.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        views.addWidget(self.record_mode, 1)
        self.settings_button = button('Hide settings', self._toggle_settings)
        views.addWidget(self.settings_button)
        self.preview_button = button('Preview mode', self._toggle_preview_mode)
        views.addWidget(self.preview_button)
        view_button = W.QPushButton('View')
        menu = W.QMenu(view_button)
        self.options_action = menu.addAction('Camera options')
        self.log_action = menu.addAction('Message log')
        for action, key, default in ((self.options_action, 'options', True), (self.log_action, 'log', False)):
            action.setCheckable(True)
            action.setChecked(self.settings.value('layout/' + key, default, type=bool))
            action.toggled.connect(lambda checked, k=key: self._set_panel_preference(k, checked))
        menu.addSeparator()
        menu.addAction('Reset layout', self._reset_layout)
        view_button.setMenu(menu)
        views.addWidget(view_button)
        rows.addLayout(views)
        root.layout().insertWidget(0, controls)
        self.layout_controls = controls
        self.settings_scroll.setMinimumWidth(240)
        self.settings_scroll.setMaximumWidth(480)
        self.body_layout.removeWidget(self.settings_scroll)
        self.body_layout.removeWidget(self.content_widget)
        self.body_splitter = W.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.body_splitter.setChildrenCollapsible(False)
        self.body_splitter.setHandleWidth(8)
        self.body_splitter.addWidget(self.settings_scroll)
        self.body_splitter.addWidget(self.content_widget)
        self.body_splitter.setStretchFactor(1, 1)
        self.body_layout.addWidget(self.body_splitter)
        width = self.settings.value('layout/sidebar_width', 290, type=int)
        self.body_splitter.setSizes([max(240, min(480, width)), 900])
        self.body_splitter.splitterMoved.connect(self._save_sidebar_width)
        # Inactive audio/review pages must not impose their wide toolbar size
        # on the camera page. Their own scrollbars keep every control reachable.
        for index in range(1, self.tabs.count()):
            page, title = self.tabs.widget(index), self.tabs.tabText(index)
            self.tabs.removeTab(index)
            scroll = W.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            scroll.setMinimumSize(0, 0)
            self.tabs.insertTab(index, scroll, title)
        self.tabs.setCurrentIndex(0)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.setStyleSheet('QTabBar::tab { padding: 6px 8px; }')
        self.footer_widget.hide()
        # Export actions remain available through View without a wide footer.
        menu.addSeparator()
        menu.addAction('Open last session', self.open_last)
        menu.addAction('Cage ROI export…', self.open_cage_roi)
        menu.addAction('Export last session MP3', self.export_last)
        menu.addAction('Play last MP3', self.play_last)
        self._settings_visible = self.settings.value('layout/settings', True, type=bool)
        self._escape_shortcut = QtGui.QShortcut(QtGui.QKeySequence('Escape'), self)
        self._escape_shortcut.activated.connect(self._escape_view)
        self._apply_panel_visibility()

    def _fit_screen(self):
        area = self.screen().availableGeometry()
        width, height = min(1420, area.width() - 24), min(950, area.height() - 48)
        self.resize(width, height)
        self.move(area.x() + (area.width() - width) // 2,
                  area.y() + (area.height() - height) // 2)

    def _save_sidebar_width(self, *args):
        if self.settings_scroll.isVisible():
            self.settings.setValue('layout/sidebar_width', self.body_splitter.sizes()[0])

    def _restore_sidebar_width(self):
        width = max(240, min(480, self.settings.value('layout/sidebar_width', 290, type=int)))
        self.body_splitter.setSizes([width, max(1, self.body_splitter.width() - width - 8)])

    def _set_panel_preference(self, key, checked):
        self.settings.setValue('layout/' + key, checked)
        self._apply_panel_visibility()

    def _apply_panel_visibility(self):
        focus = self._preview_mode
        self.settings_scroll.setVisible(self._settings_visible and not focus)
        self.settings_button.setText('Hide settings' if self._settings_visible and not focus else 'Show settings')
        self.camera_options.setVisible(self.options_action.isChecked() and not focus)
        self.log_box.setVisible(self.log_action.isChecked() and not focus)
        self.metrics_widget.setVisible(not focus)
        self.sync_summary.setVisible(not focus)
        self.tabs.tabBar().setVisible(not focus)
        self.preview_button.setText('Exit preview' if focus else 'Preview mode')
        self._refresh_presentation()
        self._reflow_timer.start(0)

    def _toggle_settings(self):
        if self._preview_mode:
            self._toggle_preview_mode()
            self._settings_visible = True
        else:
            self._settings_visible = not self._settings_visible
        self.settings.setValue('layout/settings', self._settings_visible)
        self._apply_panel_visibility()

    def _toggle_preview_mode(self):
        self._preview_mode = not self._preview_mode
        if self._preview_mode:
            self._previous_tab = self.tabs.currentIndex()
            self.tabs.setCurrentIndex(0)
        else:
            self.tabs.setCurrentIndex(getattr(self, '_previous_tab', 0))
        self._apply_panel_visibility()

    def _enlarge_camera(self, key):
        self._focused_camera = None if self._focused_camera == key else key
        self._reflow_previews()

    def _escape_view(self):
        if self._focused_camera is not None:
            self._focused_camera = None
            self._reflow_previews()
        elif self._preview_mode:
            self._toggle_preview_mode()

    def _reset_layout(self):
        self._preview_mode = False
        self._focused_camera = None
        self._settings_visible = True
        self.settings.setValue('layout/settings', True)
        self.options_action.setChecked(True)
        self.log_action.setChecked(False)
        self.body_splitter.setSizes([290, max(280, self.width() - 310)])
        self._save_sidebar_width()
        self._layout_signature = None
        self.tabs.setCurrentIndex(0)
        if hasattr(self, 'audio_toggle'):
            self.audio_toggle.setChecked(False)
            for section, expanded in ((self.session_section, True), (self.device_section, True),
                                      (self.seek_section, False), (self.audio_section, False)):
                section.toggle.setChecked(expanded)
        self._apply_panel_visibility()

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.Resize and hasattr(self, 'preview_scroll'):
            if watched is self.preview_scroll.viewport():
                self._reflow_timer.start(0)
        elif event.type() == QtCore.QEvent.Type.MouseButtonDblClick:
            for key, preview in self._preview_labels.items():
                if watched is preview:
                    self._enlarge_camera(key)
                    return True
        return super().eventFilter(watched, event)

    def _reflow_previews(self):
        return camera_previews.reflow_previews(self)

    def _recording_options(self):
        index = self.record_mode.currentIndex()
        return dict(recording_mode={2: 'flir_mp4', 3: 'microphone_mp3', 4: 'seek_only'}.get(index, 'combined'),
                    microphone=index in (1, 3))

    def _mode_changed(self):
        if self.controller.state == 'idle':
            self.controller.configure(**self._recording_options())
        self.update_controls()

    def _sync_options(self):
        self.controller.configure(label=self.session_label.text(), output=self.output.text(),
                                  sample_mode=('custom', 'all', 'after_shutter')[self.sample_mode.currentIndex()],
                                  seek_calibration=self.calibration.currentData(),
                                  sample_hz=self.sample_hz.value(), **self._recording_options())
        if self.engine:
            self.engine.config.label = self.session_label.text()

    def update_controls(self):
        if not hasattr(self, 'record'):
            return
        controller = self.controller
        busy = controller.state != 'idle'
        locked = busy or controller.armed or self._closing
        connected = self.engine is not None
        self.record.setEnabled(controller.ready() and not busy and not self._closing)
        self.stop.setEnabled(controller.is_recording and not self._closing)
        for item in (self.mode, self.devices, self.refresh, self.serial_number, self.pre, self.auto_mp3):
            item.setEnabled(not connected and not locked and self._mic_future is None)
        self.devices.setEnabled(not connected and not locked and self.mode.currentIndex() == 0 and self._mic_future is None)
        self.connect_button.setEnabled(not busy and not controller.armed and not self._closing and self._mic_future is None)
        self.connect_button.setText('Working…' if self._mic_future else 'Disconnect mic' if connected else 'Connect & monitor')
        for item in (self.session_label, self.output, self.output_button):
            item.setEnabled(not locked and controller.run_dir is None)
        self.arm.setEnabled(not self._closing and (controller.armed or
                            (controller.ready() and not busy)))
        self.tcp_port.setEnabled(self.software_trigger is None and not self._closing)
        self.ports.setEnabled(self.serial_trigger is None and not self._closing)
        if hasattr(self, 'record_mode'):
            self.record_mode.setEnabled(not locked)
            combined = self.record_mode.currentIndex() in (0, 1, 4)
            self.sample_mode.setEnabled(not locked and combined)
            self.sample_hz.setEnabled(not locked and combined and self.sample_mode.currentIndex() == 0)
            self.calibration.setEnabled(not locked and combined)
            for item in (self.shutter_mode, self.shutter_hz, self.apply_shutter_button):
                item.setEnabled(not locked)
            self.retry_cameras.setEnabled(not controller.cameras_ready and not controller.connecting and
                                         (controller.manager is None or controller.seek_ready) and not locked)
            mp3_only = self.record_mode.currentIndex() == 3
            self.pre.setEnabled(self.pre.isEnabled() and not mp3_only)
            self.auto_mp3.setEnabled(self.auto_mp3.isEnabled() and not mp3_only)
            if hasattr(self, 'audio_output_note'):
                self.audio_output_note.setText('Normal-speed MP3 only. Conversion finishes after Stop.' if mp3_only else
                                              'Original WAV + optional 10× slower MP3.\nWAV files split every 30 minutes (~720 MB).')
        self._refresh_presentation()

    def toggle_connection(self):
        if self._mic_future is not None:
            return
        if self.engine:
            self.disconnect()
            return
        demo = self.mode.currentIndex() == 1
        device = {'name': 'Synthetic demo', 'host_api': 'none'} if demo else self.devices.currentData()
        if not device:
            self.error('No microphone selected. Camera-only recording remains available.')
            return
        config = RecordingConfig(output=Path(self.output.text()).expanduser().resolve(),
                                 label=self.session_label.text(), serial=self.serial_number.text(),
                                 pre_seconds=self.pre.value(), auto_mp3=self.auto_mp3.isChecked(),
                                 device=dict(device), demo=demo)
        def connect():
            engine = SynchronizedAudioEngine(config)
            source = DemoSource(engine) if demo else create_input_source(engine, device)
            try:
                source.start()
                calibrate_source_clock(source, engine)
                return engine, source
            except Exception:
                source.stop()
                engine.close()
                raise
        self._mic_action = 'connect'
        self._mic_future = self._mic_executor.submit(connect)
        self.update_controls()

    def disconnect(self):
        if self._mic_future or self.engine is None:
            return
        if self.controller.state != 'idle':
            self.log('Finish the current take before disconnecting the microphone.')
            return
        source, engine = self.source, self.engine
        def disconnect():
            source.stop()
            engine.close()
        self._mic_action = 'disconnect'
        self._mic_future = self._mic_executor.submit(disconnect)
        self.update_controls()

    def _finish_mic_action(self):
        if self._mic_future is None or not self._mic_future.done():
            return
        future, action = self._mic_future, self._mic_action
        self._mic_future = None
        try:
            result = future.result()
            if action == 'connect':
                self.engine, self.source = result
                self.controller.audio = self.engine
                self.reset_stft()
                self.live.image.clear()
                self.log('Synthetic audio connected (DEMO).' if self.engine.config.demo else
                         'Microphone connected: 200,000 Hz, mono, 16-bit. Capture timestamps are estimates.')
            else:
                self.engine = self.source = self.controller.audio = None
                self.quality.setText('Mic disconnected')
                self.log('Microphone disconnected. Camera recording remains available.')
        except Exception as exc:
            self.log(f'Microphone {action} failed: {exc}. Camera-only recording remains available.')
        self.update_controls()

    def command(self, command):
        request_ns = time.perf_counter_ns()
        if command == 'START' and self.controller.state == 'idle':
            try:
                self._sync_options()
            except Exception as exc:
                self.error(str(exc))
                return
        result = self.controller.request(command, request_monotonic_ns=request_ns)
        if not result['ok']:
            self.error(result['error'])
        self.update_controls()

    def toggle_arm(self, checked):
        try:
            if checked:
                self._sync_options()
            result = self.controller.request('ARM' if checked else 'DISARM')
            if not result['ok']:
                raise RuntimeError(result['error'])
            if checked and self.serial_trigger:
                self.serial_trigger.sync_gate()
            self.log('Shared triggers armed.' if checked else 'Triggers disarmed; current take stopped.')
        except Exception as exc:
            self.arm.blockSignals(True)
            self.arm.setChecked(False)
            self.arm.blockSignals(False)
            self.controller.request('DISARM')
            self.error(str(exc))
        self.update_controls()

    def toggle_tcp(self):
        if self.software_trigger:
            self.controller.request('DISARM')
            self.software_trigger.close()
            self.software_trigger = None
            self.tcp_button.setText('Enable software trigger')
        else:
            try:
                self.software_trigger = SoftwareTrigger(lambda: self.controller, self.tcp_port.value())
                self.tcp_button.setText('Disable software trigger')
                self.log(f'Shared trigger listener: 127.0.0.1:{self.software_trigger.port}. Arm before START.')
            except Exception as exc:
                self.error(str(exc))
        self.update_controls()

    def toggle_serial(self):
        if self.serial_trigger:
            self.controller.request('DISARM')
            self.serial_trigger.close()
            self.serial_trigger = None
            self.serial_button.setText('Connect TTL bridge')
        else:
            try:
                self.serial_trigger = SerialTrigger(self.ports.currentText().strip(),
                                                    lambda: self.controller, self.messages.put)
                self.serial_button.setText('Disconnect TTL bridge')
                self.log('TTL bridge connected. Wait for READY, then arm shared triggers.')
            except Exception as exc:
                self.error(str(exc))
        self.update_controls()

    def refresh_ports(self):
        try:
            super().refresh_ports()
        except ImportError:
            self.ports.clear()
            self.ports.addItem('Serial support unavailable')

    def _calibration_changed(self):
        if self.controller.state != 'idle':
            return
        profile = get_seek_calibration(self.calibration.currentData())
        self.controller.configure(seek_calibration=profile.name)
        self.settings.setValue('seek/calibration', profile.name)
        description = ('Save the camera temperatures without a room correction.' if profile.name == 'none' else
                       f'True temperature (°C) = {profile.scale} × camera temperature (°C) + {profile.offset_c}.')
        self.calibration.setToolTip(description + '\nApplies to seek_thermal.csv; saved with each take.')

    def _apply_shutter(self):
        try:
            self.controller.apply_shutter(self.shutter_mode.currentText(), self.shutter_hz.value())
            self.log('Seek shutter settings applied.')
        except Exception as exc:
            self.error(str(exc))

    def _camera_action(self, action):
        try:
            if getattr(self.core, 'demo', False):
                self.log(f'{action}: synthetic preview (no camera hardware).')
            else:
                self.controller.camera_action(action)
                self.log(f'Seek {action} updated.')
        except Exception as exc:
            self.error(str(exc))

    def drain_notices(self):
        if not self.engine:
            return
        while True:
            try:
                notice = self.engine.notices.get_nowait()
            except queue.Empty:
                break
            if notice['type'] == 'started':
                self.log(f"Audio recording: {notice['path']}")
            elif notice['type'] == 'saved':
                self._audio_saved = notice
            elif notice['type'] == 'error':
                self.log(notice['message'])
                if self.controller.is_recording and self.controller.options['microphone']:
                    self.controller.fault(notice['message'])

    def _handle_camera_events(self):
        while True:
            try:
                event = self.core.ui_events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == 'connected':
                prefix = 'DEMO cameras · ' if getattr(self.core, 'demo', False) else ''
                seek = 'Seek + ' if self.controller.seek_ready else ''
                self.camera_status.setText(prefix + f'{seek}{len(event[1])} FLIR camera(s) connected')
                for index, camera in enumerate(event[1], 1):
                    key = f"flir_{camera['index']}"
                    if key not in self._preview_labels:
                        self._add_camera_panel(key, f"FLIR {camera['index']} · {camera['serial']}", index)
                self.log(self.camera_status.text())
            elif kind == 'connect_error':
                self.camera_status.setText('Camera connection failed')
                self._show_notice(event[1], 'error')
                self.log(event[1])
            elif kind == 'recording_pending':
                self._audio_saved = None
                self.log('Record requested: ' + self.record_mode.currentText())
                self.settings.setValue('output', self.output.text())
                self.settings.setValue('label', self.session_label.text())
            elif kind == 'recording_started':
                self.log('Recording selected streams.')
            elif kind == 'recording_finalizing':
                self.log('Stopping at the shared boundary and finishing files…')
            elif kind == 'recording_saved':
                self.drain_notices()
                take = event[3]
                self._last_result_take = take['take_id']
                result = {'complete': 'Saved', 'complete_with_gaps': 'Saved with gaps', 'error': 'Failed'}.get(event[2], event[2])
                self._last_result = f"Take {take['take_id']} · {result}"
                self._export_status = ''
                if event[2] != 'complete':
                    self._show_notice(self._last_result + '. Check diagnostics and the take report.',
                                      'error' if event[2] == 'error' else 'warning')
                self.log(f"Take {take['take_id']}: {event[2]}. Folder: {event[1]}")
                if take.get('recording_mode', 'combined') != 'combined':
                    self.sync_summary.setText(f"Take {take['take_id']}: {event[2]}. Folder: {event[1]}")
                    self.sync_summary.setToolTip('')
                    if take.get('mp3_path'):
                        self._last_mp3 = Path(event[1]) / take['mp3_path']
                        self.last_session = None
                        self.log(f'Normal-speed MP3 ready: {self._last_mp3}')
                    for error in take['errors']:
                        self.log(error)
                    continue
                parts = [f"{name}: {item['offset_from_seek_ns'] / 1e6:+.3f} ms" +
                         (' (estimate)' if name == 'microphone' else '')
                         for name, item in take['first_streams'].items()]
                self.sync_summary.setText('First saved offsets from Seek — ' + ' | '.join(parts) +
                                         f". Report: take{take['take_id']:02d}_sync.json")
                self.sync_summary.setToolTip(take['timing_note'])
                if event[2] == 'complete' and take.get('audio_path'):
                    self.last_session = Path(event[1]) / take['audio_path']
                    self._last_mp3 = None
                    if event[4]:
                        self.queue_export(self.last_session)
            elif kind in ('camera_error', 'writer_error', 'seek_disconnected'):
                message = event[1] if len(event) > 1 else 'Seek disconnected.'
                self._show_notice(message, 'error')
                mode = self.controller.options['recording_mode']
                seek_error = kind == 'seek_disconnected' or str(message).startswith('Seek')
                if kind == 'writer_error' or mode == 'combined' or (mode == 'flir_mp4' and not seek_error) or (mode == 'seek_only' and seek_error):
                    self.controller.fault(str(message))
                else:
                    self.log(str(message))
                if kind == 'seek_disconnected':
                    self.controller.cameras_ready = False
                    self.controller.seek_ready = False
                    self.camera_status.setText('Seek disconnected · restart after reconnecting')
            elif kind in ('message', 'writer_warning'):
                self.log(event[1])
                if kind == 'writer_warning':
                    self._show_notice(event[1])
            elif kind == 'seek_event':
                self.log(f'Seek {event[2]}: {event[3]}')
            elif kind == 'shutdown_complete':
                self._cameras_closed = True

    def _render_camera_previews(self):
        return camera_previews.render_camera_previews(self)

    def tick(self):
        self._finish_mic_action()
        super().tick()
        self._handle_camera_events()
        self._render_camera_previews()
        self.arm.blockSignals(True)
        self.arm.setChecked(self.controller.armed)
        self.arm.blockSignals(False)
        demo = getattr(self.core, 'demo', False) or bool(self.engine and self.engine.config.demo)
        self.badge.setText('CLOSING' if self._closing else ('DEMO · ' if demo else '') + self.controller.state.upper() +
                           ' · ' + self.record_mode.currentText().upper())
        if self.engine:
            state = self.engine.snapshot()
            self.quality.setToolTip('Timestamp source: ' + str(state.get('timestamp_source', 'pending')) +
                                   '. Capture timestamps are estimates; see audio_timing.csv and the take report.')
        else:
            self.quality.setText('Cameras only')
            self.quality.setToolTip('Microphone disconnected; camera recording is available.')
        if self.engine and self.source and time.monotonic() >= self._next_clock_check and not self._mic_future:
            self._next_clock_check = time.monotonic() + 1
            try:
                calibrate_source_clock(self.source, self.engine)
            except Exception as exc:
                self.engine.fault(f'Audio clock access failed: {exc}')
        if self.engine and self.engine.snapshot()['error'] and self.controller.is_recording and self.controller.options['microphone']:
            self.controller.fault(self.engine.snapshot()['error'])
        if self.controller.take and self.controller.take['shared_start_monotonic_ns'] is not None:
            take = self.controller.take
            end = take['shared_stop_monotonic_ns'] or time.perf_counter_ns()
            seconds = max(0, (end - take['shared_start_monotonic_ns']) / 1e9)
            self.elapsed.setText(f'{int(seconds) // 60:02d}:{seconds % 60:05.2f}')
        if self._closing and self._cameras_closed and self._mic_future is None:
            if self.engine:
                self.disconnect()
            elif not self.exports and self.review_future is None:
                self._shutdown_complete = True
                self.close()
        self.update_controls()

    def set_review_file(self, path):
        super().set_review_file(path)
        if self.review_path == path:
            self.tabs.setCurrentIndex(2)

    def open_last(self):
        if self.controller.run_dir:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.controller.run_dir)))

    def open_cage_roi(self):
        from ultramic_studio.recorder.roi.gui import RoiWindow
        if self._roi_window is None:
            self._roi_window = RoiWindow(self, coordinator=self.controller)
            root = self.controller.run_dir
            if (root and self.controller.state == 'idle' and self.controller.finished.is_set()
                    and (root / 'seek_thermal.csv').is_file() and (root / 'seek_log.csv').is_file()):
                self._roi_window.load_recording(str(root))
        self._roi_window.show()
        self._roi_window.raise_()
        self._roi_window.activateWindow()

    def play_last(self):
        if self._last_mp3 and self._last_mp3.exists():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self._last_mp3)))
        else:
            super().play_last()

    def export_last(self):
        if self._last_mp3 and self._last_mp3.exists():
            self.log(f'Normal-speed MP3 already saved: {self._last_mp3}')
        else:
            super().export_last()

    def closeEvent(self, event):
        if self._roi_window is not None and self._roi_window.thread is not None:
            event.ignore()
            self._roi_window.cancel_export()
            if not self._roi_close_pending:
                self._roi_close_pending = True
                self._roi_window.thread.finished.connect(self.close)
            self.log('Closing: waiting for the cage export to stop safely.')
            return
        if self._roi_window is not None:
            self._roi_window.close()
        if self._shutdown_complete:
            self.timer.stop()
            if self.serial_trigger:
                self.serial_trigger.close()
            if self.software_trigger:
                self.software_trigger.close()
            self.background.shutdown(wait=True)
            self._mic_executor.shutdown(wait=True)
            event.accept()
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            self.log('Closing: finishing the current take and queued exports.')
            self.controller.shutdown()
            self.update_controls()


class RecorderApp:
    def __init__(self, core, *, demo_audio=False):
        self.core = core
        self.demo_audio = demo_audio

    def run(self):
        pg.setConfigOptions(antialias=False, imageAxisOrder='row-major')
        app = W.QApplication.instance() or W.QApplication(sys.argv)
        app.setStyle('Fusion')
        self.window = UnifiedWindow(self.core, demo_audio=self.demo_audio)
        self.window.show()
        return app.exec()
