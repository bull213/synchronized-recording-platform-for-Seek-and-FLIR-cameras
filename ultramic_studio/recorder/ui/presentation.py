"""Recorder-only layout and read-only presentation of acquisition state."""
from __future__ import annotations

from contextlib import nullcontext
from PySide6 import QtCore as C, QtWidgets as W


def readiness(controller, sample_mode, closing=False):
    """Return start availability, a human explanation, and a navigation target.

    Mirrors the existing start gates; never configures or opens a device.
    """
    if controller is None:
        return False, 'Waiting for controller initialization.', ''
    lock = getattr(controller, 'lock', None)
    ctx = lock if lock is not None else nullcontext()
    with ctx:
        if closing:
            return False, 'Closing: finishing recordings and background tasks.', ''
        state = getattr(controller, 'state', 'idle')
        if state == 'finalizing':
            return False, 'Finishing files. Wait before starting the next take.', ''
        if state == 'starting':
            return False, 'Starting: waiting for the first selected frames.', ''
        if state == 'recording':
            return False, 'Recording selected devices. Stop finishes this take.', ''
        if getattr(controller, 'roi_export_active', False):
            return False, 'Cage export is running. Wait for it to finish before recording.', ''
        if getattr(controller, '_writer_unhealthy', False):
            return False, 'Recording writer unavailable. See diagnostics before restarting.', 'log'
        recovery_busy = getattr(controller, '_recovery_busy', None)
        busy = recovery_busy() if callable(recovery_busy) else False
        prep_done = getattr(controller, 'prepare_done', None)
        done = prep_done.is_set() if (prep_done is not None and hasattr(prep_done, 'is_set')) else True
        if busy or not done:
            return False, 'Waiting for camera recovery or previous take cleanup.', 'log'
        options = getattr(controller, 'options', {}) or {}
        mode = options.get('recording_mode', 'combined')
        seek_ready = getattr(controller, 'seek_ready', False)
        flir_cams = getattr(controller, 'flir_cams', [])
        if mode in ('combined', 'seek_only') and not seek_ready:
            return False, 'Waiting for Seek. Check the camera connection.', 'devices'
        if mode in ('combined', 'flir_mp4') and not flir_cams:
            return False, 'Waiting for FLIR. Check the camera connection.', 'devices'
        mic_selected = options.get('microphone', False)
        if mic_selected:
            audio_ready = getattr(controller, '_audio_ready', None)
            ready_audio = audio_ready() if callable(audio_ready) else False
            if not ready_audio:
                return False, 'Connect the microphone and wait for audio.', 'microphone'
        shutter_settings = getattr(controller, 'shutter_settings', {}) or {}
        auto_shutter = shutter_settings.get('automatic', True) if isinstance(shutter_settings, dict) else True
        if mode in ('combined', 'seek_only') and sample_mode == 'after_shutter' and auto_shutter:
            return False, 'After-shutter sampling needs Custom shutter. Apply shutter settings.', 'seek'
        ready_fn = getattr(controller, 'ready', None)
        try:
            is_ready = ready_fn() if callable(ready_fn) else True
        except Exception as exc:
            return False, f'Waiting for selected devices ({exc}).', 'devices'
        if not is_ready:
            return False, 'Waiting for the selected devices to become ready.', 'devices'
        return True, 'Ready to record the selected devices.', ''


def tone(widget, value):
    """Update a widget's 'tone' dynamic property and refresh style polishing."""
    if widget is None:
        return
    try:
        if widget.property('tone') != value:
            widget.setProperty('tone', value)
            style = widget.style()
            if style is not None:
                style.unpolish(widget)
                style.polish(widget)
    except Exception:
        pass


class Section(W.QWidget):
    """Collapsible disclosure panel preserving layout state across sessions."""

    def __init__(self, title, settings, key, expanded=False):
        super().__init__()
        self.settings, self.key = settings, key
        box = W.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(5)
        self.toggle = W.QPushButton(title)
        self.toggle.setObjectName('disclosure')
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(f"{title} ({'expanded' if expanded else 'collapsed'})")
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
        val = expanded
        if self.settings is not None:
            try:
                val = self.settings.value('layout/v2/' + key, expanded, type=bool)
            except Exception:
                val = expanded
        self.toggle.setChecked(bool(val))
        self.set_expanded(self.toggle.isChecked())

    def set_expanded(self, expanded):
        self.toggle.setText(('▾  ' if expanded else '▸  ') + self.title)
        self.toggle.setAccessibleName(f"{self.title} ({'expanded' if expanded else 'collapsed'})")
        self.content.setVisible(expanded)
        self.summary.setVisible(not expanded)
        if self.settings is not None:
            try:
                self.settings.setValue('layout/v2/' + self.key, expanded)
            except Exception:
                pass

    def field(self, title, widget):
        caption = W.QLabel(title)
        caption.setBuddy(widget)
        caption.setWordWrap(True)
        widget.setAccessibleName(title)
        self.form.addWidget(caption)
        self.form.addWidget(widget)


class RecorderPresentation:
    """Presentation mixin for recording status, banner warnings, and sidebars."""

    def log(self, text):
        super().log(text)
        if hasattr(self, '_export_status'):
            msg = str(text)
            if msg.startswith('Audible MP3 ready:'):
                self._export_status = 'Audible MP3 saved'
            elif msg.startswith('MP3 export failed'):
                self._export_status = 'MP3 failed; WAV retained'
                self._show_notice(text, 'error')
            elif msg.startswith('Microphone ') and 'failed:' in msg:
                self._show_notice(text, 'error')

    def error(self, text):
        if hasattr(self, '_notice'):
            self._show_notice(text, 'error')
        super().error(text)

    # -------------------------------------------------------------------------
    # Layout Builders
    # -------------------------------------------------------------------------

    def _build_recorder_presentation(self):
        """Build and dock all presentation panels while reusing acquisition widgets."""
        self._init_presentation_state()
        self._setup_transport_labels()
        self._build_notice_bar()
        self._build_sidebar_sections()
        self._build_live_audio_dock()
        self._build_result_widget()
        self._configure_tab_labels()
        self._place_live_audio()
        self._refresh_presentation()

    def _init_presentation_state(self):
        self._notice = ''
        self._notice_tone = 'warning'
        self._last_result = None
        self._last_result_take = None
        self._export_status = ''

    def _setup_transport_labels(self):
        self.stop.setObjectName('stop')
        self.record.setText('●  Record')
        self.record.setAccessibleName('Record')
        self.record.setToolTip('Start recording selected devices (Ctrl+R / F5)')
        self.stop.setText('■  Stop')
        self.stop.setAccessibleName('Stop recording')
        self.stop.setToolTip('Finish and save current take (Ctrl+Shift+S)')
        self.trigger_status = W.QLabel('Triggers disarmed')
        self.trigger_status.setObjectName('triggerStatus')
        self.trigger_status.setAccessibleName('Trigger status')
        self.layout_controls.layout().itemAt(0).layout().addWidget(self.trigger_status)

    def _build_notice_bar(self):
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
        self.readiness_action.setAccessibleName('Diagnostics')
        self.readiness_action.clicked.connect(self._show_status_target)
        bar.addWidget(self.readiness_action)
        self.dismiss_notice = W.QPushButton('Dismiss')
        self.dismiss_notice.setAccessibleName('Dismiss notice')
        self.dismiss_notice.clicked.connect(self._dismiss_notice)
        bar.addWidget(self.dismiss_notice)
        self.layout_controls.layout().addWidget(self.notice_bar)

    def _build_sidebar_sections(self):
        # Session section
        self.session_section = Section('Session', self.settings, 'session', True)
        self.session_section.field('Session label', self.session_label)
        self.session_section.field('Recording folder', self.output)
        self.session_section.form.addWidget(self.output_button)
        self.output_button.setText('Browse folder…')
        self.output_button.setAccessibleName('Browse recording folder')
        self.lock_note = W.QLabel()
        self.lock_note.setWordWrap(True)
        self.session_section.form.addWidget(self.lock_note)
        self.new_cohort_button = W.QPushButton('New cohort / change session…')
        self.new_cohort_button.setObjectName('newCohort')
        self.new_cohort_button.setToolTip('Finalize the current session folder and unlock session label for the next cohort.')
        self.new_cohort_button.setAccessibleName('Start new cohort and unlock session settings')
        self.new_cohort_button.clicked.connect(self._new_cohort_clicked)
        self.new_cohort_button.setVisible(False)
        self.session_section.form.addWidget(self.new_cohort_button)

        # Device section
        self.device_section = Section('Devices', self.settings, 'devices', True)
        self.device_status = W.QLabel()
        self.device_status.setObjectName('deviceStatus')
        self.device_status.setWordWrap(True)
        self.device_status.setAccessibleName('Connected devices and selection status')
        self.device_section.form.addWidget(self.device_status)
        self.device_section.form.addWidget(self.retry_cameras)
        self.mic_controls = W.QWidget()
        mic = W.QVBoxLayout(self.mic_controls)
        mic.setContentsMargins(0, 0, 0, 0)
        mic.addWidget(W.QLabel('Microphone input'))
        for widget in (self.mode, self.devices, self.refresh, self.connect_button):
            mic.addWidget(widget)
        self.device_section.form.addWidget(self.mic_controls)

        # Seek options section
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
        if old_options is not None:
            old_options.deleteLater()
        self.camera_options.setMaximumHeight(16777215)
        self.camera_options.setMinimumHeight(0)
        self.camera_options.setWidget(self.seek_section)
        self.camera_options.setVerticalScrollBarPolicy(C.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.camera_options.setHorizontalScrollBarPolicy(C.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.camera_options.setSizePolicy(W.QSizePolicy.Policy.Preferred, W.QSizePolicy.Policy.Minimum)

        # Audio settings section
        self.audio_section = Section('Audio settings', self.settings, 'audio')
        self.audio_section.field('Microphone identifier', self.serial_number)
        self.audio_section.field('Pre-trigger audio', self.pre)
        self.auto_mp3.setText('Create audible MP3 after recording')
        self.audio_section.form.addWidget(self.auto_mp3)
        self.audio_section.form.addWidget(self.audio_output_note)

        # Host sidebar container
        side = W.QWidget()
        layout = W.QVBoxLayout(side)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        for widget in (self.session_section, self.device_section, self.camera_options, self.audio_section):
            layout.addWidget(widget)
        layout.addStretch()
        old_side = self.settings_scroll.takeWidget()
        self.settings_scroll.setWidget(side)
        if old_side is not None:
            old_side.deleteLater()

    def _build_live_audio_dock(self):
        self.audio_toggle = W.QCheckBox('Show live audio dock below cameras')
        self.audio_toggle.setObjectName('audioDockToggle')
        self.audio_toggle.setToolTip('Display live audio spectrogram directly beneath camera previews. Full FFT and frequency controls remain in the Live audio (full) tab.')
        self.audio_toggle.setAccessibleName('Show live audio dock below cameras')
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

    def _build_result_widget(self):
        result = W.QWidget()
        row = W.QHBoxLayout(result)
        row.setContentsMargins(0, 0, 0, 0)
        self.result_label = W.QLabel('No completed take yet')
        self.result_label.setAccessibleName('Recording result')
        self.result_label.setWordWrap(True)
        row.addWidget(self.result_label, 1)
        self.open_recording = W.QPushButton('Open recording folder')
        self.open_recording.setAccessibleName('Open recording folder')
        self.open_recording.setToolTip('Open the session recording directory in the file manager.')
        self.open_recording.clicked.connect(self.open_last)
        row.addWidget(self.open_recording)
        self.content_widget.layout().insertWidget(2, result)
        self.result_widget = result
        self.metrics_widget.hide()

    def _configure_tab_labels(self):
        self.tabs.setTabText(0, 'Cameras')
        self.tabs.setTabText(1, 'Live audio (full)')
        self.tabs.setTabText(2, 'Audio file review')
        self.log_action.setText('Diagnostics')
        self.options_action.setText('Seek settings')

    # -------------------------------------------------------------------------
    # Audio Docking
    # -------------------------------------------------------------------------

    def _audio_dock_changed(self, checked):
        if self.settings is not None:
            try:
                self.settings.setValue('layout/v2/live_audio', checked)
            except Exception:
                pass
        self._place_live_audio()

    def _place_live_audio(self, *_):
        if not hasattr(self, 'audio_dock') or not hasattr(self, 'live') or not hasattr(self.live, 'plot'):
            return
        docked = self.tabs.currentIndex() == 0 and self.audio_toggle.isChecked() and not self._preview_mode
        if docked and self.audio_dock.widget() is not self.live.plot:
            self.live.layout().removeWidget(self.live.plot)
            self.audio_dock.setWidget(self.live.plot)
        elif not docked and self.audio_dock.widget() is self.live.plot:
            self.audio_dock.takeWidget()
            insert_at = min(1, self.live.layout().count())
            self.live.layout().insertWidget(insert_at, self.live.plot)
        self.audio_dock.setVisible(docked)
        if docked and not getattr(self, '_audio_sized', False):
            self.monitor_splitter.setSizes([500, 220])
            self._audio_sized = True

    # -------------------------------------------------------------------------
    # Navigation and Notices
    # -------------------------------------------------------------------------

    def _show_status_target(self):
        target = getattr(self, '_status_target', '')
        if not target:
            return
        if target == 'log':
            if getattr(self, '_preview_mode', False):
                self._toggle_preview_mode()
            if hasattr(self, 'log_action'):
                self.log_action.setChecked(True)
            if hasattr(self, 'log_box') and self.log_box.isVisible():
                self.log_box.setFocus()
            return
        if getattr(self, '_preview_mode', False) or not getattr(self, '_settings_visible', True):
            self._toggle_settings()
        if target == 'seek':
            section = self.seek_section
            if hasattr(self, 'options_action'):
                self.options_action.setChecked(True)
            control = self.shutter_mode
        elif target == 'microphone':
            section = self.device_section
            control = self.connect_button
        elif target == 'devices':
            section = self.device_section
            control = self.retry_cameras
        elif target == 'session':
            section = self.session_section
            control = self.session_label if not (self.session_label.text() or '').strip() else self.output_button
        else:
            section = self.device_section
            control = self.retry_cameras
        if hasattr(section, 'toggle'):
            section.toggle.setChecked(True)
        self._refresh_presentation()
        if hasattr(self, 'settings_scroll') and control is not None:
            C.QTimer.singleShot(0, lambda: self._scroll_and_focus(control))
        elif control is not None and control.isEnabled():
            control.setFocus()

    def _scroll_and_focus(self, control):
        try:
            if hasattr(self, 'settings_scroll') and control is not None:
                self.settings_scroll.ensureWidgetVisible(control, 12, 12)
                if control.isVisible() and control.isEnabled():
                    control.setFocus()
        except (RuntimeError, Exception):
            pass

    def _dismiss_notice(self):
        self._notice = ''
        self._refresh_presentation()

    def _show_notice(self, message, severity='warning'):
        self._notice, self._notice_tone = str(message) if message else '', severity
        if hasattr(self, 'readiness_label'):
            self._refresh_presentation()

    # -------------------------------------------------------------------------
    # Refresh Pipeline
    # -------------------------------------------------------------------------

    def _refresh_presentation(self):
        """Orchestrate presentation updates across all status banners and summaries."""
        if not hasattr(self, 'result_widget'):
            return
        ctl = self.controller
        mode = max(0, self.record_mode.currentIndex())
        sample_idx = self.sample_mode.currentIndex()
        sample_name = ('custom', 'all', 'after_shutter')[sample_idx] if 0 <= sample_idx < 3 else 'custom'
        enabled, explanation, target = readiness(ctl, sample_name, getattr(self, '_closing', False))

        self._update_readiness_banner(ctl, enabled, explanation, target)
        state_text = self._update_state_badges(ctl, enabled)
        self._update_lock_and_cohort_controls(ctl)
        self._update_section_summaries(mode)
        self._update_panel_visibilities(mode)
        self._update_device_health_summary(ctl, mode)
        self._update_result_summary(ctl, state_text)
        self._place_live_audio()

    def _update_readiness_banner(self, ctl, enabled, explanation, target):
        self.record.setEnabled(enabled)
        self.record.setToolTip(explanation)
        self._status_target = 'log' if self._notice else target
        clean_notice = ' '.join(str(self._notice).split()).strip() if self._notice else ''
        notice_preview = (clean_notice[:177] + '…') if len(clean_notice) > 180 else clean_notice
        self.readiness_label.setText(explanation + (f'  •  {notice_preview}' if notice_preview else ''))
        self.readiness_label.setAccessibleName('Recording readiness and warnings: ' + self.readiness_label.text())
        self.readiness_label.setToolTip(self._notice if self._notice else explanation)
        tone(self.readiness_label, self._notice_tone if self._notice else 'normal' if enabled or getattr(ctl, 'is_recording', False) else 'warning')
        self.readiness_action.setVisible(bool(self._status_target))
        self.readiness_action.setText('Diagnostics' if self._status_target == 'log' else 'Show settings')
        self.readiness_action.setToolTip('Open diagnostics and logs' if self._status_target == 'log' else 'Navigate to relevant settings')
        self.readiness_action.setAccessibleName('Diagnostics log' if self._status_target == 'log' else 'Show settings')
        self.dismiss_notice.setVisible(bool(self._notice))
        self.dismiss_notice.setToolTip('Dismiss this temporary warning notice')
        self.dismiss_notice.setAccessibleName('Dismiss warning notice')

    def _update_state_badges(self, ctl, enabled):
        armed = getattr(ctl, 'armed', False)
        self.trigger_status.setText('Triggers ARMED' if armed else 'Triggers disarmed')
        self.trigger_status.setAccessibleName(self.trigger_status.text())
        tone(self.trigger_status, 'warning' if armed else 'normal')
        state = getattr(ctl, 'state', 'idle')
        state_text = {'starting': 'Starting', 'recording': 'Recording', 'finalizing': 'Finishing files'}.get(state, 'Ready' if enabled else 'Not ready')
        demo = getattr(self.core, 'demo', False) or bool(self.engine and getattr(self.engine.config, 'demo', False))
        self.badge.setStyleSheet('')
        self.badge.setText(('DEMO  ·  ' if demo else '') + ('Closing' if getattr(self, '_closing', False) else state_text))
        tone(self.badge, 'recording' if state == 'recording' else 'success' if enabled else 'warning')
        return state_text

    def _update_lock_and_cohort_controls(self, ctl):
        state = getattr(ctl, 'state', 'idle')
        armed = getattr(ctl, 'armed', False)
        has_run = bool(getattr(ctl, 'run_dir', None))
        idle = state == 'idle' and not armed
        self.lock_note.setText('Settings locked while recording or triggers are armed.' if not idle else
                               'Session folder and label are fixed for this run. Click "New cohort" or restart to start a new session.' if has_run else '')
        self.lock_note.setVisible(bool(self.lock_note.text()))
        if hasattr(self, 'new_cohort_button'):
            self.new_cohort_button.setVisible(has_run and idle)
            self.new_cohort_button.setEnabled(has_run and idle)

    def _update_section_summaries(self, mode):
        sess_text = (self.session_label.text() or '').strip()
        sess_summary = (sess_text[:40] + '…') if len(sess_text) > 40 else sess_text
        self.session_section.summary.setText(sess_summary or 'Choose a session label')
        self.session_section.summary.setToolTip(sess_text if len(sess_text) > 40 else '')
        self.seek_section.summary.setText(f'{self.sample_mode.currentText()} · {self.sample_hz.value():g} Hz\n{self.calibration.currentText()}')
        self.audio_section.summary.setText('200 kHz · mono · ' + ('normal-speed MP3' if mode == 3 else 'WAV + optional audible MP3'))
        self.device_section.summary.setText(self.record_mode.currentText())

    def _update_panel_visibilities(self, mode):
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
        self.result_label.setToolTip(self.sync_summary.text() if self.sync_summary.text() else '')

    def _update_device_health_summary(self, ctl, mode):
        with ctl.lock:
            health = {key: (val.get('state', 'unknown') if isinstance(val, dict) else str(val)) for key, val in getattr(ctl, 'health', {}).items()}
            cameras = list(getattr(ctl, 'flir_cams', []))
        entries = [('Seek', 'seek', getattr(ctl, 'seek_ready', False), mode in (0, 1, 4))]
        entries += [(f"FLIR {cam.get('index', i)}", f"flir_cam{cam.get('index', i)}", True, mode in (0, 1, 2)) for i, cam in enumerate(cameras)]
        if not cameras:
            entries.append(('FLIR', 'flir', False, mode in (0, 1, 2)))
        lines = []
        for name, key, connected, selected in entries:
            status = health.get(key, 'connected' if connected else 'disconnected')
            if status == 'healthy':
                status = 'connected'
            lines.append(f'{name}: {status} · ' + ('selected' if selected else 'monitor only'))
            panel_key = key.replace('flir_cam', 'flir_')
            if hasattr(self, '_device_labels') and panel_key in self._device_labels:
                self._device_labels[panel_key].setText(status.capitalize() + ' · ' + ('selected' if selected else 'monitor only'))
                tone(self._device_labels[panel_key], 'normal' if status == 'connected' else 'warning')
        mic_status = 'connecting' if getattr(self, '_mic_future', None) else 'connected' if getattr(self, 'engine', None) else 'disconnected'
        lines.append('Microphone: ' + mic_status + (' · selected' if mode in (1, 3) else ' · not selected'))
        self.device_status.setText('\n'.join(lines))
        self.device_status.setAccessibleName('Device status:\n' + '\n'.join(lines))

    def _update_result_summary(self, ctl, state_text):
        state = getattr(ctl, 'state', 'idle')
        if getattr(self, '_session_reset', False):
            text = 'Session finalized. Enter new session details above.'
        elif getattr(ctl, 'take', None) and self._last_result_take == getattr(ctl, 'take_number', 0) and state == 'idle':
            text = self._last_result or 'No completed take yet'
        else:
            text = f'Take {getattr(ctl, "take_number", 0)} · {state_text.lower()}' if state != 'idle' else 'No completed take yet'
        if getattr(self, 'exports', None):
            text += '  ·  Creating audible MP3…'
        elif self._export_status:
            text += '  ·  ' + self._export_status
        self.result_label.setText(text)
        self.result_label.setAccessibleName('Recording result: ' + text)
        self.open_recording.setEnabled(ctl.run_dir is not None)
        self.result_widget.setVisible(not self._preview_mode or bool(self._last_result) or bool(getattr(self, 'exports', None)) or bool(self._export_status))

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def _new_cohort_clicked(self):
        if self.controller.state != 'idle' or self.controller.armed:
            return
        try:
            self.controller.reset_session()
            self._last_result = None
            self._last_result_take = None
            self._session_reset = True
            if hasattr(self, '_update_status_bar'):
                self._update_status_bar()
        except Exception as exc:
            self.error(f'Could not start new cohort: {exc}')
        self.update_controls()
        if hasattr(self, 'session_section') and hasattr(self.session_section, 'toggle'):
            self.session_section.toggle.setChecked(True)
        if hasattr(self, 'session_label') and self.session_label.isEnabled():
            self.session_label.setFocus()
            self.session_label.selectAll()
