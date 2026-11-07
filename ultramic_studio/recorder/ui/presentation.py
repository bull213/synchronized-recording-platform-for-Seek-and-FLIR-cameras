"""Recorder-only layout and read-only presentation of acquisition state."""
from PySide6 import QtCore as C, QtWidgets as W


def readiness(controller, sample_mode, closing=False):
    """Return start availability, a human explanation, and a navigation target.

    Mirrors the existing start gates; never configures or opens a device.
    """
    with controller.lock:
        if closing:
            return False, 'Closing: finishing recordings and background tasks.', ''
        if controller.state == 'finalizing':
            return False, 'Finishing files. Wait before starting the next take.', ''
        if controller.state == 'starting':
            return False, 'Starting: waiting for the first selected frames.', ''
        if controller.state == 'recording':
            return False, 'Recording selected devices. Stop finishes this take.', ''
        if getattr(controller, 'roi_export_active', False):
            return False, 'Cage export is running. Wait for it to finish before recording.', ''
        if controller._writer_unhealthy:
            return False, 'Recording writer unavailable. See diagnostics before restarting.', 'log'
        if controller._recovery_busy() or not controller.prepare_done.is_set():
            return False, 'Waiting for camera recovery or previous take cleanup.', 'log'
        mode = controller.options['recording_mode']
        if mode in ('combined', 'seek_only') and not controller.seek_ready:
            return False, 'Waiting for Seek. Check the camera connection.', 'devices'
        if mode in ('combined', 'flir_mp4') and not controller.flir_cams:
            return False, 'Waiting for FLIR. Check the camera connection.', 'devices'
        if controller.options['microphone'] and not controller._audio_ready():
            return False, 'Connect the microphone and wait for audio.', 'microphone'
        if mode in ('combined', 'seek_only') and sample_mode == 'after_shutter' and controller.shutter_settings['automatic']:
            return False, 'After-shutter sampling needs Custom shutter. Apply shutter settings.', 'seek'
        if not controller.ready():
            return False, 'Waiting for the selected devices to become ready.', 'devices'
        return True, 'Ready to record the selected devices.', ''


def tone(widget, value):
    if widget.property('tone') != value:
        widget.setProperty('tone', value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


class Section(W.QWidget):
    def __init__(self, title, settings, key, expanded=False):
        super().__init__()
        self.settings, self.key = settings, key
        box = W.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(5)
        self.toggle = W.QPushButton(title)
        self.toggle.setObjectName('disclosure')
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(title)
        self.title = title
        box.addWidget(self.toggle)
        self.summary = W.QLabel()
        self.summary.setObjectName('sectionSummary')
        self.summary.setWordWrap(True)
        box.addWidget(self.summary)
        self.content = W.QWidget()
        self.form = W.QVBoxLayout(self.content)
        self.form.setContentsMargins(4, 4, 4, 8)
        box.addWidget(self.content)
        self.toggle.toggled.connect(self.set_expanded)
        self.toggle.setChecked(settings.value('layout/v2/' + key, expanded, type=bool))
        self.set_expanded(self.toggle.isChecked())

    def set_expanded(self, expanded):
        self.toggle.setText(('▾  ' if expanded else '▸  ') + self.title)
        self.content.setVisible(expanded)
        self.summary.setVisible(not expanded)
        self.settings.setValue('layout/v2/' + self.key, expanded)

    def field(self, title, widget):
        caption = W.QLabel(title)
        caption.setBuddy(widget)
        widget.setAccessibleName(title)
        self.form.addWidget(caption)
        self.form.addWidget(widget)


class RecorderPresentation:
    def log(self, text):
        super().log(text)
        if hasattr(self, '_export_status'):
            if str(text).startswith('Audible MP3 ready:'):
                self._export_status = 'Audible MP3 saved'
            elif str(text).startswith('MP3 export failed'):
                self._export_status = 'MP3 failed; WAV retained'
                self._show_notice(text, 'error')
            elif str(text).startswith('Microphone ') and 'failed:' in str(text):
                self._show_notice(text, 'error')

    def error(self, text):
        if hasattr(self, '_notice'):
            self._show_notice(text, 'error')
        super().error(text)

    def _build_recorder_presentation(self):
        # All acquisition widgets are reused, preserving signals and values.
        self._notice = ''
        self._notice_tone = 'warning'
        self._last_result = None
        self._last_result_take = None
        self._export_status = ''
        self.stop.setObjectName('stop')
        self.record.setText('●  Record')
        self.record.setAccessibleName('Record')
        self.stop.setText('■  Stop')
        self.stop.setAccessibleName('Stop recording')
        self.trigger_status = W.QLabel('Triggers disarmed')
        self.trigger_status.setObjectName('triggerStatus')
        self.trigger_status.setAccessibleName('Trigger status')
        self.layout_controls.layout().itemAt(0).layout().addWidget(self.trigger_status)

        # The status banner is outside scroll areas and remains in preview mode.
        self.notice_bar = W.QWidget()
        bar = W.QHBoxLayout(self.notice_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        self.readiness_label = W.QLabel()
        self.readiness_label.setObjectName('notice')
        self.readiness_label.setWordWrap(True)
        self.readiness_label.setAccessibleName('Recording readiness and warnings')
        self.readiness_label.setMinimumWidth(0)
        bar.addWidget(self.readiness_label, 1)
        self.readiness_action = W.QPushButton('Details')
        self.readiness_action.clicked.connect(self._show_status_target)
        bar.addWidget(self.readiness_action)
        self.dismiss_notice = W.QPushButton('Dismiss')
        self.dismiss_notice.clicked.connect(self._dismiss_notice)
        bar.addWidget(self.dismiss_notice)
        self.layout_controls.layout().addWidget(self.notice_bar)

        self.session_section = Section('Session', self.settings, 'session', True)
        self.session_section.field('Session label', self.session_label)
        self.session_section.field('Recording folder', self.output)
        self.session_section.form.addWidget(self.output_button)
        self.output_button.setText('Browse folder…')
        self.lock_note = W.QLabel()
        self.lock_note.setWordWrap(True)
        self.session_section.form.addWidget(self.lock_note)

        self.device_section = Section('Devices', self.settings, 'devices', True)
        self.device_status = W.QLabel()
        self.device_status.setObjectName('deviceStatus')
        self.device_status.setWordWrap(True)
        self.device_section.form.addWidget(self.device_status)
        self.device_section.form.addWidget(self.retry_cameras)
        self.mic_controls = W.QWidget()
        mic = W.QVBoxLayout(self.mic_controls)
        mic.setContentsMargins(0, 0, 0, 0)
        mic.addWidget(W.QLabel('Microphone input'))
        for widget in (self.mode, self.devices, self.refresh, self.connect_button):
            mic.addWidget(widget)
        self.device_section.form.addWidget(self.mic_controls)

        self.seek_section = Section('Seek settings', self.settings, 'seek')
        self.seek_section.field('Sampling', self.sample_mode)
        self.seek_section.field('Sampling rate', self.sample_hz)
        self.seek_section.field('Shutter mode', self.shutter_mode)
        self.seek_section.field('Shutter rate', self.shutter_hz)
        self.seek_section.form.addWidget(self.apply_shutter_button)
        self.seek_section.field('Temperature calibration', self.calibration)
        for widget in self.preview_actions:
            self.seek_section.form.addWidget(widget)
        old_options = self.camera_options.takeWidget()
        old_options.deleteLater()
        self.camera_options.setMaximumHeight(16777215)
        self.camera_options.setMinimumHeight(0)
        self.camera_options.setWidget(self.seek_section)
        # One outer scrollbar; the section itself expands to its content.
        self.camera_options.setVerticalScrollBarPolicy(C.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.camera_options.setHorizontalScrollBarPolicy(C.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.camera_options.setSizePolicy(W.QSizePolicy.Policy.Preferred, W.QSizePolicy.Policy.Minimum)

        self.audio_section = Section('Audio settings', self.settings, 'audio')
        self.audio_section.field('Microphone identifier', self.serial_number)
        self.audio_section.field('Pre-trigger audio', self.pre)
        self.auto_mp3.setText('Create audible MP3 after recording')
        self.audio_section.form.addWidget(self.auto_mp3)
        self.audio_section.form.addWidget(self.audio_output_note)

        side = W.QWidget()
        layout = W.QVBoxLayout(side)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        for widget in (self.session_section, self.device_section, self.camera_options, self.audio_section):
            layout.addWidget(widget)
        layout.addStretch()
        old_side = self.settings_scroll.takeWidget()
        self.settings_scroll.setWidget(side)
        old_side.deleteLater()

        self.audio_toggle = W.QCheckBox('Show live audio')
        self.audio_toggle.setToolTip('Monitor the same live spectrogram here. Open Live audio for FFT, frequency and display controls.')
        self.audio_toggle.setChecked(self.settings.value('layout/v2/live_audio', False, type=bool))
        self.monitor_page.layout().insertWidget(1, self.audio_toggle)
        self.audio_toggle.toggled.connect(self._audio_dock_changed)
        self.monitor_splitter = W.QSplitter(C.Qt.Orientation.Vertical)
        self.monitor_splitter.setChildrenCollapsible(False)
        camera_layout = self.monitor_page.layout()
        camera_layout.removeWidget(self.preview_scroll)
        camera_layout.insertWidget(2, self.monitor_splitter, 1)
        self.monitor_splitter.addWidget(self.preview_scroll)
        self.audio_dock = W.QScrollArea()
        self.audio_dock.setWidgetResizable(True)
        self.audio_dock.setMinimumSize(0, 0)
        self.monitor_splitter.addWidget(self.audio_dock)
        self.audio_tab_scroll = self.tabs.widget(1)
        self.live.plot.setMinimumHeight(150)
        self.tabs.currentChanged.connect(self._place_live_audio)

        result = W.QWidget()
        row = W.QHBoxLayout(result)
        row.setContentsMargins(0, 0, 0, 0)
        self.result_label = W.QLabel('No completed take yet')
        self.result_label.setAccessibleName('Recording result')
        self.result_label.setWordWrap(True)
        row.addWidget(self.result_label, 1)
        self.open_recording = W.QPushButton('Open recording folder')
        self.open_recording.clicked.connect(self.open_last)
        row.addWidget(self.open_recording)
        self.content_widget.layout().insertWidget(2, result)
        self.result_widget = result
        self.metrics_widget.hide()
        self.tabs.setTabText(0, 'Cameras')
        self.tabs.setTabText(1, 'Live audio')
        self.tabs.setTabText(2, 'Review audio')
        self.log_action.setText('Diagnostics')
        self.options_action.setText('Seek settings')
        self._place_live_audio()
        self._refresh_presentation()

    def _audio_dock_changed(self, checked):
        self.settings.setValue('layout/v2/live_audio', checked)
        self._place_live_audio()

    def _place_live_audio(self, *_):
        if not hasattr(self, 'audio_dock'):
            return
        docked = self.tabs.currentIndex() == 0 and self.audio_toggle.isChecked() and not self._preview_mode
        # Move the plot, not its settings toolbars. A single plot and STFT are
        # shared by both views, leaving useful plotting space on small screens.
        if docked and self.audio_dock.widget() is not self.live.plot:
            self.live.layout().removeWidget(self.live.plot)
            self.audio_dock.setWidget(self.live.plot)
        elif not docked and self.audio_dock.widget() is self.live.plot:
            self.audio_dock.takeWidget()
            self.live.layout().insertWidget(1, self.live.plot)
        self.audio_dock.setVisible(docked)
        if docked and not getattr(self, '_audio_sized', False):
            self.monitor_splitter.setSizes([500, 220])
            self._audio_sized = True

    def _show_status_target(self):
        target = self._status_target
        if target == 'log':
            if self._preview_mode:
                self._toggle_preview_mode()
            self.log_action.setChecked(True)
            return
        if self._preview_mode or not self._settings_visible:
            self._toggle_settings()
        section = self.seek_section if target == 'seek' else self.device_section
        if target == 'seek':
            self.options_action.setChecked(True)
        section.toggle.setChecked(True)
        control = self.shutter_mode if target == 'seek' else self.connect_button if target == 'microphone' else self.retry_cameras
        self._refresh_presentation()
        C.QTimer.singleShot(0, lambda: self.settings_scroll.ensureWidgetVisible(control, 12, 12))
        control.setFocus()

    def _dismiss_notice(self):
        self._notice = ''
        self._refresh_presentation()

    def _show_notice(self, message, severity='warning'):
        self._notice, self._notice_tone = str(message), severity

    def _refresh_presentation(self):
        if not hasattr(self, 'result_widget'):
            return
        ctl = self.controller
        mode = self.record_mode.currentIndex()
        enabled, explanation, target = readiness(ctl, ('custom', 'all', 'after_shutter')[self.sample_mode.currentIndex()], self._closing)
        self.record.setEnabled(enabled)
        self.record.setToolTip(explanation)
        self._status_target = 'log' if self._notice else target
        self.readiness_label.setText(explanation + ('  •  ' + self._notice if self._notice else ''))
        self.readiness_label.setAccessibleName('Recording readiness and warnings: ' + self.readiness_label.text())
        tone(self.readiness_label, self._notice_tone if self._notice else 'normal' if enabled or ctl.is_recording else 'warning')
        self.readiness_action.setVisible(bool(self._status_target))
        self.readiness_action.setText('Diagnostics' if self._status_target == 'log' else 'Show settings')
        self.dismiss_notice.setVisible(bool(self._notice))
        self.trigger_status.setText('Triggers ARMED' if ctl.armed else 'Triggers disarmed')
        self.trigger_status.setAccessibleName(self.trigger_status.text())
        tone(self.trigger_status, 'warning' if ctl.armed else 'normal')
        state_text = {'starting': 'Starting', 'recording': 'Recording', 'finalizing': 'Finishing files'}.get(ctl.state, 'Ready' if enabled else 'Not ready')
        demo = getattr(self.core, 'demo', False) or bool(self.engine and self.engine.config.demo)
        self.badge.setStyleSheet('')
        self.badge.setText(('DEMO  ·  ' if demo else '') + ('Closing' if self._closing else state_text))
        tone(self.badge, 'recording' if ctl.state == 'recording' else 'success' if enabled else 'warning')
        self.lock_note.setText('Settings locked while recording or triggers are armed.' if ctl.state != 'idle' or ctl.armed else
                               'Session folder and label are fixed for this run. Restart the app to change them.' if ctl.run_dir else '')
        self.lock_note.setVisible(bool(self.lock_note.text()))
        self.session_section.summary.setText(self.session_label.text() or 'Choose a session label')
        self.seek_section.summary.setText(f'{self.sample_mode.currentText()} · {self.sample_hz.value():g} Hz\n{self.calibration.currentText()}')
        self.audio_section.summary.setText('200 kHz · mono · ' + ('normal-speed MP3' if mode == 3 else 'WAV + optional audible MP3'))
        self.mic_controls.setVisible(mode in (1, 3) or self.engine is not None)
        self.audio_section.setVisible(mode in (1, 3) or self.engine is not None)
        self.audio_toggle.setVisible(mode in (1, 3) or self.engine is not None)
        self.seek_section.setVisible(mode in (0, 1, 4))
        self.camera_options.setVisible(self.options_action.isChecked() and mode in (0, 1, 4) and not self._preview_mode)
        self.camera_options.setMinimumHeight(self.seek_section.sizeHint().height() if self.camera_options.isVisible() else 0)
        self.metrics_widget.setVisible(self.tabs.currentIndex() == 1 and not self._preview_mode)
        compact = self.height() < 620
        self.camera_status.setVisible(not compact or self._preview_mode)
        self.sync_summary.setVisible(not compact and not self._preview_mode)
        self.result_label.setToolTip(self.sync_summary.text())
        with ctl.lock:
            health = {key: value['state'] for key, value in ctl.health.items()}
            cameras = list(ctl.flir_cams)
        entries = [('Seek', 'seek', ctl.seek_ready, mode in (0, 1, 4))]
        entries += [(f"FLIR {cam['index']}", f"flir_cam{cam['index']}", True, mode in (0, 1, 2)) for cam in cameras]
        if not cameras:
            entries.append(('FLIR', 'flir', False, mode in (0, 1, 2)))
        lines = []
        for name, key, connected, selected in entries:
            status = health.get(key, 'connected' if connected else 'disconnected')
            if status == 'healthy':
                status = 'connected'
            lines.append(f'{name}: {status} · ' + ('selected' if selected else 'monitor only'))
            panel_key = key.replace('flir_cam', 'flir_')
            if panel_key in self._device_labels:
                self._device_labels[panel_key].setText(status.capitalize() + ' · ' + ('selected' if selected else 'monitor only'))
                tone(self._device_labels[panel_key], 'normal' if status == 'connected' else 'warning')
        mic_status = 'connecting' if self._mic_future else 'connected' if self.engine else 'disconnected'
        lines.append('Microphone: ' + mic_status + (' · selected' if mode in (1, 3) else ' · not selected'))
        self.device_status.setText('\n'.join(lines))
        self.device_section.summary.setText(self.record_mode.currentText())
        if ctl.take and self._last_result_take == ctl.take_number and ctl.state == 'idle':
            text = self._last_result or 'No completed take yet'
        else:
            text = f'Take {ctl.take_number} · {state_text.lower()}' if ctl.state != 'idle' else 'No completed take yet'
        if self.exports:
            text += '  ·  Creating audible MP3…'
        elif self._export_status:
            text += '  ·  ' + self._export_status
        self.result_label.setText(text)
        self.result_label.setAccessibleName('Recording result: ' + text)
        self.open_recording.setEnabled(ctl.run_dir is not None)
        self.result_widget.setVisible(not self._preview_mode or bool(self._last_result) or bool(self.exports) or bool(self._export_status))
        self._place_live_audio()
