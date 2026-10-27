from __future__ import annotations

import json
import queue
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets as W

from . import __version__
from .audio import BLOCK, RATE, DemoSource, create_input_source, list_devices
from .core import RecorderEngine, RecordingConfig
from .dsp import StreamingSTFT, file_spectrogram
from .export import export_mp3, export_session
from .triggers import SerialTrigger, SoftwareTrigger


STYLE = """
QMainWindow, QWidget { background:#101823; color:#dce5f2; font-family:'Segoe UI'; font-size:13px; }
QLabel#title { font-size:25px; font-weight:650; color:#f6f8fc; }
QLabel#muted { color:#91a1b5; }
QLabel#badge { background:#233448; color:#9ce3da; border-radius:6px; padding:7px 12px; font-weight:600; }
QLabel#metric { font-family:'Consolas'; font-size:23px; color:#f3f6fb; }
QGroupBox { border:1px solid #2c3a4d; border-radius:7px; margin-top:17px; padding-top:12px; font-weight:600; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; color:#aabbd0; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background:#1b2737; border:1px solid #34455c; border-radius:5px; padding:6px; selection-background-color:#317d83; }
QComboBox QAbstractItemView { background:#1b2737; selection-background-color:#317d83; }
QPushButton { background:#26384c; border:1px solid #3b526d; border-radius:5px; padding:8px 11px; font-weight:600; }
QPushButton:hover { background:#344e68; }
QPushButton:disabled { color:#627187; background:#182330; border-color:#263444; }
QPushButton#primary { background:#2d958c; color:#071d1b; border-color:#3eb3a6; }
QPushButton#record { background:#ba475e; color:white; border-color:#d95d75; }
QPushButton#record:disabled { color:#887079; background:#402831; border-color:#4f3640; }
QCheckBox { spacing:7px; }
QCheckBox::indicator { width:14px; height:14px; border:1px solid #56718c; border-radius:3px; background:#142332; }
QCheckBox::indicator:checked { background:#45b7a9; border:2px solid #94eee0; }
QTabWidget::pane { border:1px solid #2c3a4d; border-radius:6px; }
QTabBar::tab { background:#172332; padding:10px 22px; color:#92a5bc; }
QTabBar::tab:selected { background:#24364a; color:#dcf7f2; }
QPlainTextEdit { background:#111e2b; border:1px solid #2a3b4e; border-radius:5px; color:#aabbd0; font-family:'Consolas'; font-size:11px; }
QScrollArea { border:none; }
QStatusBar { color:#99adc5; }
QToolTip { background:#21364a; color:#f1f5fc; border:1px solid #53738c; }
"""


def label(text, muted=False):
    item = W.QLabel(text)
    if muted:
        item.setObjectName("muted")
    return item


def button(text, fn, kind=None):
    item = W.QPushButton(text)
    if kind:
        item.setObjectName(kind)
    item.clicked.connect(fn)
    return item


class Spectrogram(W.QWidget):
    frequencyChanged = QtCore.Signal(float, float)

    def __init__(self):
        super().__init__()
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        frequency_row = W.QHBoxLayout()
        frequency_row.addWidget(label("Frequency"))
        frequency_row.addWidget(label("Min"))
        self.frequency_min = W.QDoubleSpinBox()
        self.frequency_max = W.QDoubleSpinBox()
        for control in (self.frequency_min, self.frequency_max):
            control.setDecimals(2)
            control.setSingleStep(1)
            control.setSuffix(" kHz")
            control.setKeyboardTracking(False)
            control.setToolTip("Type a frequency and press Enter, or use the arrows. Changes affect the view only.")
        frequency_row.addWidget(self.frequency_min)
        frequency_row.addWidget(label("Max"))
        frequency_row.addWidget(self.frequency_max)
        self.full_range = button("Full range", lambda: self.set_frequency_range(0, self.nyquist))
        frequency_row.addWidget(self.full_range)
        frequency_row.addStretch()
        self.frequency_limit = label("", True)
        frequency_row.addWidget(self.frequency_limit)
        layout.addLayout(frequency_row)
        self.plot = pg.PlotWidget(background="#0b121d")
        self.plot.setLabel("left", "Frequency", units="kHz")
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.showGrid(x=True, y=True, alpha=.13)
        self.plot.setMenuEnabled(False)
        self.image = pg.ImageItem(axisOrder="row-major")
        self.image.setAutoDownsample(True)
        self.image.setLookupTable(pg.colormap.get("inferno").getLookupTable(nPts=256))
        self.plot.addItem(self.image)
        self.bar = pg.ColorBarItem(values=(-95, -15), colorMap=pg.colormap.get("inferno"),
                                   label="dBFS", interactive=False, width=16)
        self.bar.setImageItem(self.image, insert_in=self.plot.getPlotItem())
        layout.addWidget(self.plot)
        self.cursor_text = label("Move over the spectrogram to read time and frequency.", True)
        layout.addWidget(self.cursor_text)
        self.proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved, rateLimit=20, slot=self._mouse)
        self.set_sample_rate(RATE)
        self.set_frequency_range(20, RATE / 2000)
        self.frequency_min.valueChanged.connect(self._minimum_changed)
        self.frequency_max.valueChanged.connect(self._maximum_changed)
        self.plot.getViewBox().sigYRangeChanged.connect(self._view_frequency_changed)

    @property
    def frequency_range(self):
        return self.frequency_min.value(), self.frequency_max.value()

    def set_sample_rate(self, rate):
        low, high = self.frequency_range
        # Round the editable bound down, never above the file's Nyquist limit.
        self.nyquist = np.floor(rate / 2000 * 100) / 100
        if low >= self.nyquist:
            low, high = 0, self.nyquist
        self.frequency_limit.setText(f"Limit: {rate / 2000:g} kHz")
        self.frequency_limit.setToolTip(
            f"{rate:,} samples/s: recorded frequencies extend to half the sample rate. "
            "The Ultramic 200K cannot capture 100–120 kHz at 200,000 samples/s.")
        with QtCore.QSignalBlocker(self.frequency_min), QtCore.QSignalBlocker(self.frequency_max):
            self.frequency_min.setRange(0, max(0, self.nyquist - .1))
            self.frequency_max.setRange(min(.1, self.nyquist), self.nyquist)
        self.plot.setLimits(yMin=0, yMax=self.nyquist, minYRange=min(.1, self.nyquist),
                            maxYRange=self.nyquist)
        self.set_frequency_range(low, high if high > low else self.nyquist)

    def set_frequency_range(self, low, high):
        span = min(.1, self.nyquist)
        high = min(self.nyquist, max(span, float(high)))
        low = max(0, min(float(low), high - span))
        with QtCore.QSignalBlocker(self.frequency_min), QtCore.QSignalBlocker(self.frequency_max):
            self.frequency_min.setValue(low)
            self.frequency_max.setValue(high)
        self.plot.setYRange(*self.frequency_range, padding=0)

    def _minimum_changed(self, low):
        self.set_frequency_range(low, max(self.frequency_max.value(), low + .1))

    def _maximum_changed(self, high):
        self.set_frequency_range(min(self.frequency_min.value(), high - .1), high)

    def _view_frequency_changed(self, view, bounds):
        # Keep numeric controls in sync with wheel zoom and drag-to-pan, without
        # feeding rounded values back into the plot on every mouse movement.
        with QtCore.QSignalBlocker(self.frequency_min), QtCore.QSignalBlocker(self.frequency_max):
            self.frequency_min.setValue(bounds[0])
            self.frequency_max.setValue(bounds[1])
        self.frequencyChanged.emit(*self.frequency_range)

    def _mouse(self, event):
        point = event[0]
        if self.plot.sceneBoundingRect().contains(point):
            pos = self.plot.getViewBox().mapSceneToView(point)
            self.cursor_text.setText(f"Time  {pos.x():.4f} s     Frequency  {pos.y():.2f} kHz     •     Wheel to zoom · drag to pan")

    def show_data(self, db, start, step, rate, nfft, floor, top, x_range=None):
        if not len(db):
            return
        # Display pooling is separate from raw recording and FFT computation.
        pooling = max(1, int(np.ceil(len(db) / 2200)))
        if pooling > 1:
            db = np.maximum.reduceat(db, np.arange(0, len(db), pooling), axis=0)
            step *= pooling
        df = rate / nfft / 1000
        self.image.setImage(db.T, autoLevels=False, levels=(floor, top))
        self.image.setRect(QtCore.QRectF(start, -df / 2, len(db) * step, rate / 2000 + df))
        self.bar.setLevels((floor, top))
        if x_range is not None:
            self.plot.setXRange(*x_range, padding=0)
        # Frequency limits are controlled independently of new audio frames,
        # so a user's selected range or mouse zoom survives live updates.


class MainWindow(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Ultramic Studio {__version__} · Mouse vocalization recorder")
        self.resize(1370, 900)
        self.setMinimumSize(1050, 700)
        self.setStyleSheet(STYLE)
        self.settings = QtCore.QSettings("UltramicStudio", "Recorder")
        self.engine = None
        self.source = None
        self.serial_trigger = None
        self.software_trigger = None
        self.background = ThreadPoolExecutor(max_workers=2, thread_name_prefix="Export/review")
        self.exports = {}
        self.review_future = None
        self.review_path = None
        self.review_rate = RATE
        self.review_total = 0
        self.last_session = None
        self.messages = queue.SimpleQueue()
        self.stft = StreamingSTFT()
        self.live_times = np.empty(0)
        self.live_db = np.empty((0, 257), dtype=np.float32)
        self.preview_end = None
        self.latest_peak = -120
        self._build()
        self.refresh_devices()
        self.refresh_ports()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(80)
        self.log("Ready. Select the Ultramic input, or choose the synthetic demo.")

    def _build(self):
        root = W.QWidget()
        self.setCentralWidget(root)
        outer = W.QVBoxLayout(root)
        outer.setContentsMargins(20, 15, 20, 12)
        self.header_widget = W.QWidget()
        header = W.QHBoxLayout(self.header_widget)
        header.setContentsMargins(0, 0, 0, 0)
        title_box = W.QVBoxLayout()
        title = label("Ultramic Studio")
        title.setObjectName("title")
        title_box.addWidget(title)
        title_box.addWidget(label("Ultrasonic vocalizations  /  live capture + review", True))
        header.addLayout(title_box)
        header.addStretch()
        self.badge = label("DISCONNECTED")
        self.badge.setObjectName("badge")
        header.addWidget(self.badge)
        outer.addWidget(self.header_widget)
        body = W.QHBoxLayout()
        self.body_layout = body
        outer.addLayout(body, 1)
        scroll = W.QScrollArea()
        self.settings_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(335)
        side = W.QWidget()
        side_layout = W.QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 10, 0)
        scroll.setWidget(side)
        body.addWidget(scroll)

        source_group = W.QGroupBox("01   INPUT")
        grid = W.QVBoxLayout(source_group)
        self.mode = W.QComboBox()
        self.mode.addItems(["Microphone · 200 kHz", "Demo · synthetic calls"])
        self.mode.currentIndexChanged.connect(self.update_controls)
        grid.addWidget(self.mode)
        self.devices = W.QComboBox()
        self.devices.setMinimumContentsLength(10)
        self.devices.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        grid.addWidget(self.devices)
        self.refresh = button("Refresh audio devices", self.refresh_devices)
        grid.addWidget(self.refresh)
        row = W.QHBoxLayout()
        row.addWidget(label("Mic serial"))
        self.serial_number = W.QLineEdit("")
        self.serial_number.setPlaceholderText("Optional identifier")
        self.serial_number.setToolTip("User-entered identifier, saved in metadata. The audio driver does not verify this serial number.")
        row.addWidget(self.serial_number)
        grid.addLayout(row)
        grid.addWidget(label("200,000 Hz  ·  16-bit PCM  ·  mono", True))
        self.connect_button = button("Connect & monitor", self.toggle_connection, "primary")
        grid.addWidget(self.connect_button)
        side_layout.addWidget(source_group)

        files = W.QGroupBox("02   RECORDING")
        grid = W.QVBoxLayout(files)
        self.session_label = W.QLineEdit(self.settings.value("label", "mouse_01"))
        self.session_label.setPlaceholderText("Animal / session label")
        grid.addWidget(self.session_label)
        self.output = W.QLineEdit(self.settings.value("output", str(Path.home() / "Documents" / "Ultramic_Recordings")))
        grid.addWidget(self.output)
        self.output_button = button("Choose recording folder…", self.choose_output)
        grid.addWidget(self.output_button)
        pre_row = W.QHBoxLayout()
        pre_row.addWidget(label("Pre-trigger"))
        self.pre = W.QDoubleSpinBox()
        self.pre.setRange(0, 5)
        self.pre.setSingleStep(.1)
        self.pre.setValue(.5)
        self.pre.setSuffix(" s")
        pre_row.addWidget(self.pre)
        grid.addLayout(pre_row)
        self.auto_mp3 = W.QCheckBox("Create audible MP3 after each recording")
        self.auto_mp3.setChecked(True)
        grid.addWidget(self.auto_mp3)
        note = label("Original WAV + optional 10× slower MP3.\nWAV files split every 30 minutes (~720 MB).", True)
        note.setWordWrap(True)
        grid.addWidget(note)
        record_row = W.QHBoxLayout()
        self.record = button("●  Record", lambda: self.command("START"), "record")
        self.stop = button("■  Stop", lambda: self.command("STOP"))
        self.stop.setToolTip("Stops this recording; armed triggers remain active.")
        record_row.addWidget(self.record)
        record_row.addWidget(self.stop)
        grid.addLayout(record_row)
        side_layout.addWidget(files)

        triggers = W.QGroupBox("03   EXTERNAL TRIGGERS")
        grid = W.QVBoxLayout(triggers)
        self.arm = W.QCheckBox("Arm start / stop triggers")
        self.arm.toggled.connect(self.toggle_arm)
        self.arm.setToolTip("Disarming also stops the current recording. Both trigger sources control the same recorder.")
        grid.addWidget(self.arm)
        tcp_row = W.QHBoxLayout()
        self.tcp_port = W.QSpinBox()
        self.tcp_port.setRange(1024, 65535)
        self.tcp_port.setValue(8765)
        tcp_row.addWidget(label("Local TCP port"))
        tcp_row.addWidget(self.tcp_port)
        grid.addLayout(tcp_row)
        self.tcp_button = button("Enable software trigger", self.toggle_tcp)
        grid.addWidget(self.tcp_button)
        port_row = W.QHBoxLayout()
        self.ports = W.QComboBox()
        self.ports.setEditable(True)
        self.ports.setMinimumContentsLength(5)
        port_row.addWidget(self.ports, 1)
        port_row.addWidget(button("Refresh", self.refresh_ports))
        grid.addLayout(port_row)
        self.serial_button = button("Connect TTL bridge", self.toggle_serial)
        grid.addWidget(self.serial_button)
        trigger_note = label("TTL uses the supplied Arduino bridge,\nor a serial device sending START / STOP.\nUSB trigger timing is not sample-accurate.", True)
        trigger_note.setWordWrap(True)
        grid.addWidget(trigger_note)
        side_layout.addWidget(triggers)
        side_layout.addStretch()

        self.content_widget = W.QWidget()
        right = W.QVBoxLayout(self.content_widget)
        right.setContentsMargins(0, 0, 0, 0)
        body.addWidget(self.content_widget, 1)
        self.metrics_widget = W.QWidget()
        metrics = W.QHBoxLayout(self.metrics_widget)
        metrics.setContentsMargins(0, 0, 0, 0)
        self.elapsed = self.metric_panel(metrics, "RECORDING DURATION", "00:00.00")
        self.peak = self.metric_panel(metrics, "INPUT PEAK", "— dBFS")
        self.quality = self.metric_panel(metrics, "CAPTURE", "Idle")
        right.addWidget(self.metrics_widget)
        self.tabs = W.QTabWidget()
        right.addWidget(self.tabs, 1)
        live = W.QWidget()
        live_layout = W.QVBoxLayout(live)
        options = W.QHBoxLayout()
        options.addWidget(label("FFT"))
        self.fft = W.QComboBox()
        self.fft.addItems(["256", "512", "1024", "2048"])
        self.fft.setCurrentText("512")
        self.fft.currentTextChanged.connect(self.reset_stft)
        options.addWidget(self.fft)
        options.addWidget(label("Window"))
        self.history = W.QSpinBox()
        self.history.setRange(2, 20)
        self.history.setValue(6)
        self.history.setSuffix(" s")
        options.addWidget(self.history)
        options.addWidget(label("Floor"))
        self.floor = W.QSpinBox()
        self.floor.setRange(-130, -35)
        self.floor.setValue(-95)
        self.floor.setSuffix(" dB")
        options.addWidget(self.floor)
        self.pause = W.QCheckBox("Freeze display")
        options.addWidget(self.pause)
        options.addStretch()
        live_layout.addLayout(options)
        self.live = Spectrogram()
        live_layout.addWidget(self.live, 1)
        self.resolution = label("", True)
        live_layout.addWidget(self.resolution)
        self.tabs.addTab(live, "Live spectrogram")

        review = W.QWidget()
        review_layout = W.QVBoxLayout(review)
        review_controls = W.QHBoxLayout()
        review_controls.addWidget(button("Open WAV…", self.open_wav))
        review_controls.addWidget(label("From"))
        self.review_start = W.QDoubleSpinBox()
        self.review_start.setDecimals(3)
        self.review_start.setRange(0, 1e9)
        self.review_start.setSuffix(" s")
        review_controls.addWidget(self.review_start)
        self.review_span = W.QDoubleSpinBox()
        self.review_span.setRange(.1, 60)
        self.review_span.setValue(6)
        self.review_span.setSuffix(" s view")
        review_controls.addWidget(self.review_span)
        self.review_load = button("Load window", self.load_review)
        review_controls.addWidget(self.review_load)
        review_controls.addStretch()
        review_layout.addLayout(review_controls)
        self.review_name = label("Open a recorded WAV to inspect calls at their original frequencies.", True)
        self.review_name.setWordWrap(True)
        review_layout.addWidget(self.review_name)
        self.review = Spectrogram()
        review_layout.addWidget(self.review, 1)
        for name, spectrogram in (("live", self.live), ("review", self.review)):
            spectrogram.set_frequency_range(
                self.settings.value(f"frequency/{name}/min_khz", 20, type=float),
                self.settings.value(f"frequency/{name}/max_khz", 100, type=float))
            spectrogram.frequencyChanged.connect(
                lambda low, high, view=name: self.save_frequency_range(view, low, high))
        actions = W.QHBoxLayout()
        actions.addWidget(button("Export this WAV as audible MP3…", self.export_review))
        actions.addWidget(label("MP3 frequency ÷ 10; duration × 10. WAV stays unchanged.", True))
        actions.addStretch()
        review_layout.addLayout(actions)
        self.tabs.addTab(review, "Review recording")
        self.footer_widget = W.QWidget()
        footer = W.QHBoxLayout(self.footer_widget)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(button("Open last session", self.open_last))
        footer.addWidget(button("Export last session MP3", self.export_last))
        footer.addWidget(button("Play last MP3", self.play_last))
        footer.addStretch()
        right.addWidget(self.footer_widget)
        self.log_box = W.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(300)
        self.log_box.setFixedHeight(85)
        right.addWidget(self.log_box)
        self.statusBar().showMessage("WAV is the analysis source. MP3 is an audible listening copy.")
        self.reset_stft()
        self.update_controls()

    def metric_panel(self, layout, title, value):
        group = W.QVBoxLayout()
        group.addWidget(label(title, True))
        item = label(value)
        item.setObjectName("metric")
        group.addWidget(item)
        layout.addLayout(group, 1)
        return item

    def save_frequency_range(self, view, low, high):
        self.settings.setValue(f"frequency/{view}/min_khz", low)
        self.settings.setValue(f"frequency/{view}/max_khz", high)

    def log(self, text):
        self.log_box.appendPlainText(time.strftime("%H:%M:%S") + "  " + str(text))

    def error(self, text):
        self.log(text)
        W.QMessageBox.warning(self, "Ultramic Studio", str(text))

    def choose_output(self):
        target = W.QFileDialog.getExistingDirectory(self, "Recording folder", self.output.text())
        if target:
            self.output.setText(target)

    def refresh_devices(self):
        selected = self.devices.currentData()
        self.devices.clear()
        try:
            for device in list_devices():
                self.devices.addItem(f"{device['name']}  [{device['host_api']}]", device)
                index = self.devices.count() - 1
                self.devices.setItemData(index, self.devices.itemText(index), QtCore.Qt.ItemDataRole.ToolTipRole)
                if selected and all(selected.get(key) == device.get(key)
                                    for key in ("index", "name", "host_api", "backend")):
                    self.devices.setCurrentIndex(index)
        except Exception as exc:
            self.log(f"Audio device discovery: {exc}")
        if not self.devices.count():
            self.devices.addItem("No audio inputs found · demo available", None)

    def refresh_ports(self):
        from serial.tools import list_ports
        current = self.ports.currentText()
        self.ports.clear()
        for port in list_ports.comports():
            self.ports.addItem(port.device)
        if current:
            self.ports.setCurrentText(current)
        elif not self.ports.count():
            self.ports.setEditText("COM3")

    def update_controls(self):
        connected = self.engine is not None
        state = self.engine.snapshot() if connected else {}
        recording = state.get("recording", False)
        self.mode.setEnabled(not connected)
        self.devices.setEnabled(not connected and self.mode.currentIndex() == 0)
        self.refresh.setEnabled(not connected)
        for item in (self.serial_number, self.pre, self.auto_mp3, self.output, self.output_button, self.session_label):
            item.setEnabled(not connected)
        self.record.setEnabled(connected and state.get("connected", False) and not recording)
        self.stop.setEnabled(recording)
        self.arm.setEnabled(connected and state.get("connected", False))
        self.connect_button.setText("Disconnect" if connected else "Connect & monitor")
        self.tcp_port.setEnabled(self.software_trigger is None)
        self.ports.setEnabled(self.serial_trigger is None)

    def reset_stft(self):
        nfft = int(self.fft.currentText())
        self.stft = StreamingSTFT(nfft=nfft)
        self.live_times = np.empty(0)
        self.live_db = np.empty((0, nfft // 2 + 1), dtype=np.float32)
        self.preview_end = None
        self.resolution.setText(f"Hann window · {RATE/nfft:.1f} Hz bins · {1000*nfft/RATE:.2f} ms window · "
                                f"{1000*(nfft//4)/RATE:.2f} ms hop · display in dBFS, not calibrated SPL")

    def toggle_connection(self):
        if self.engine:
            self.disconnect()
            return
        demo = self.mode.currentIndex() == 1
        if not self.output.text().strip():
            self.error("Choose a recording folder first.")
            return
        device = {"name": "Synthetic demo", "host_api": "none"} if demo else self.devices.currentData()
        if not device:
            self.error("No microphone selected. Connect the Ultramic, refresh devices, and select its MME, WASAPI, or WDM-KS input.")
            return
        try:
            config = RecordingConfig(
                output=Path(self.output.text()).expanduser().resolve(),
                label=self.session_label.text() or "mouse", serial=self.serial_number.text(),
                pre_seconds=self.pre.value(), auto_mp3=self.auto_mp3.isChecked(),
                device=dict(device), demo=demo)
            self.engine = RecorderEngine(config)
            self.source = DemoSource(self.engine) if demo else create_input_source(self.engine, device)
            self.reset_stft()
            self.live.image.clear()
            self.source.start()
            self.settings.setValue("output", str(config.output))
            self.settings.setValue("label", config.label)
            self.log("Synthetic demo connected. Saved sessions are marked DEMO." if demo else
                     "Microphone opened at 200,000 Hz, mono, 16-bit. Verify ultrasonic response on your hardware.")
        except Exception as exc:
            self.disconnect()
            self.error(f"Could not open 200 kHz input: {exc}\n\nClose Avisoft and other audio programs, refresh devices, and select the Ultramic's MME, WASAPI, or WDM-KS entry. The app does not fall back to 48 kHz.")
        self.update_controls()

    def disconnect(self):
        try:
            if self.source:
                self.source.stop()
            if self.engine:
                self.engine.close()
                self.drain_notices()
        except Exception as exc:
            self.error(f"Could not finish disconnecting: {exc}")
            return
        self.source = self.engine = None
        self.arm.blockSignals(True)
        self.arm.setChecked(False)
        self.arm.blockSignals(False)
        self.badge.setText("DISCONNECTED")
        self.quality.setText("Idle")
        self.log("Disconnected; recording files finalized.")
        self.update_controls()

    def command(self, command):
        if not self.engine:
            return
        result = self.engine.request(command)
        if not result["ok"]:
            self.error(result["error"])
        self.update_controls()

    def toggle_arm(self, checked):
        if not self.engine:
            return
        result = self.engine.request("ARM" if checked else "DISARM")
        if not result["ok"]:
            self.error(result["error"])
            return
        if checked and self.serial_trigger:
            try:
                self.serial_trigger.sync_gate()
            except Exception as exc:
                self.engine.fault(f"Cannot synchronize TTL bridge: {exc}")
        self.log("External triggers armed." if checked else "Triggers disarmed; recording stopped.")

    def toggle_tcp(self):
        if self.software_trigger:
            if self.engine:
                self.engine.request("DISARM")
            self.software_trigger.close()
            self.software_trigger = None
            self.tcp_button.setText("Enable software trigger")
            self.log("Software trigger disabled; triggers disarmed.")
        else:
            try:
                self.software_trigger = SoftwareTrigger(lambda: self.engine, self.tcp_port.value())
                self.tcp_button.setText("Disable software trigger")
                self.log(f"Software trigger listening on 127.0.0.1:{self.software_trigger.port}. Arm in the app before sending START.")
            except Exception as exc:
                self.error(f"Software trigger: {exc}")
        self.update_controls()

    def toggle_serial(self):
        if self.serial_trigger:
            if self.engine:
                self.engine.request("DISARM")
            self.serial_trigger.close()
            self.serial_trigger = None
            self.serial_button.setText("Connect TTL bridge")
            self.log("TTL bridge disconnected; triggers disarmed.")
        else:
            try:
                self.serial_trigger = SerialTrigger(self.ports.currentText().strip(), lambda: self.engine, self.messages.put)
                self.serial_button.setText("Disconnect TTL bridge")
                self.log("Serial port opened at 115200 baud. Allow the board to reboot, wait for READY, then arm triggers.")
            except Exception as exc:
                self.error(f"TTL serial input: {exc}")
        self.update_controls()

    def drain_notices(self):
        if not self.engine:
            return
        while True:
            try:
                notice = self.engine.notices.get_nowait()
            except queue.Empty:
                break
            if notice["type"] == "started":
                self.log(f"Recording: {Path(notice['path']).name}")
            elif notice["type"] == "saved":
                self.last_session = Path(notice["path"])
                self.log(("INCOMPLETE recording preserved: " if notice["error"] else "Saved: ") + self.last_session.name)
                if notice["auto_mp3"]:
                    self.queue_export(self.last_session)
            elif notice["type"] == "error":
                self.log("CAPTURE STOPPED: " + notice["message"])
                self.statusBar().showMessage("Capture stopped: " + notice["message"])

    def tick(self):
        while True:
            try:
                self.log(self.messages.get_nowait())
            except queue.Empty:
                break
        if self.serial_trigger and not self.serial_trigger.thread.is_alive():
            self.serial_trigger.close()
            self.serial_trigger = None
            self.serial_button.setText("Connect TTL bridge")
        if self.source:
            self.source.check_health()
        if self.engine:
            self.drain_notices()
            state = self.engine.snapshot()
            self.arm.blockSignals(True)
            self.arm.setChecked(state["armed"])
            self.arm.blockSignals(False)
            prefix = "DEMO · " if self.engine.config.demo else ""
            self.badge.setText(prefix + ("ERROR · RECONNECT" if state["error"] else
                              "● RECORDING" if state["recording"] else "ARMED" if state["armed"] else "MONITORING"))
            self.badge.setStyleSheet("color:#ff8d9e" if state["error"] else "")
            self.quality.setText("Stopped: error" if state["error"] else "200 kHz")
            seconds = state["recorded_samples"] / RATE
            self.elapsed.setText(f"{int(seconds)//60:02d}:{seconds%60:05.2f}")
            new_times, new_db = [], []
            peak = 0
            while True:
                try:
                    block = self.engine.preview.get_nowait()
                except queue.Empty:
                    break
                if self.preview_end is not None and self.preview_end != block.start:
                    self.live_times = np.empty(0)
                    self.live_db = np.empty((0, self.stft.nfft//2+1), dtype=np.float32)
                    new_times, new_db = [], []
                self.preview_end = block.start + block.frames
                x = np.frombuffer(block.pcm, dtype="<i2").astype(np.int32)
                if x.size:
                    peak = max(peak, int(np.abs(x).max()))
                times, db = self.stft.feed(block.pcm, block.start)
                if len(times):
                    new_times.append(times)
                    new_db.append(db)
            if new_times:
                self.live_times = np.concatenate([self.live_times, *new_times])
                self.live_db = np.concatenate([self.live_db, *new_db])
                keep = self.live_times >= self.live_times[-1] - self.history.value()
                self.live_times, self.live_db = self.live_times[keep], self.live_db[keep]
                self.latest_peak = 20 * np.log10(max(1, peak) / 32768)
                self.peak.setText(f"{self.latest_peak:.1f} dBFS" + (" CLIP" if peak >= 32760 else ""))
                if not self.pause.isChecked():
                    end = max(self.history.value(), self.live_times[-1])
                    self.live.show_data(self.live_db, self.live_times[0] - self.stft.hop/RATE/2,
                                        self.stft.hop/RATE, RATE, self.stft.nfft,
                                        self.floor.value(), -15,
                                        (end-self.history.value(), end))
            self.update_controls()
        for key, future in list(self.exports.items()):
            if future.done():
                del self.exports[key]
                try:
                    path = future.result()
                    self.log(f"Audible MP3 ready: {path}")
                except Exception as exc:
                    self.log(f"MP3 export failed (WAV retained): {exc}")
        if self.review_future and self.review_future.done():
            future, self.review_future = self.review_future, None
            self.review_load.setEnabled(True)
            try:
                db, step, begin, span, rate, nfft = future.result()
                self.review.show_data(db, begin + nfft/rate/2 - step/2,
                                      step, rate, nfft, self.floor.value(), -15,
                                      (begin, begin+span))
            except Exception as exc:
                self.error(f"Cannot display WAV: {exc}")

    def queue_export(self, session):
        key = str(session)
        if key not in self.exports:
            self.exports[key] = self.background.submit(export_session, session)
            self.log("Creating 10× time-expanded MP3 in the background…")

    def export_last(self):
        if self.last_session:
            self.queue_export(self.last_session)
        else:
            self.error("Record and stop a session first, or open a WAV in Review recording.")

    def open_last(self):
        if self.last_session:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.last_session)))

    def play_last(self):
        if self.last_session and (self.last_session / "listening_10x.mp3").exists():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.last_session / "listening_10x.mp3")))
        else:
            self.error("No finished MP3 yet. Export the last session, then click Play last MP3.")

    def open_wav(self):
        name, _ = W.QFileDialog.getOpenFileName(self, "Open recording", self.output.text(), "WAV recordings (*.wav)")
        if name:
            self.set_review_file(Path(name))

    def set_review_file(self, path):
        try:
            if self.review_future:
                raise RuntimeError("Wait for the current review window to finish loading.")
            if self.engine:
                state = self.engine.snapshot()
                if state["recording"] and path.resolve().parent == Path(state["session_path"]).resolve():
                    raise RuntimeError("Stop this recording before opening its WAV for review or export.")
            with wave.open(str(path), "rb") as f:
                if f.getsampwidth() != 2 or f.getnchannels() != 1:
                    raise ValueError("This viewer supports mono 16-bit PCM WAV.")
                self.review_rate, self.review_total = f.getframerate(), f.getnframes()
                if not self.review_total:
                    raise ValueError("This WAV contains no samples.")
            self.review_path = path
            self.review.set_sample_rate(self.review_rate)
            duration = self.review_total / self.review_rate
            self.review_start.setMaximum(max(0, duration - 1/self.review_rate))
            self.review_start.setValue(0)
            self.review_name.setText(f"{path.name} · {self.review_rate:,} Hz · {duration:.3f} s · mono 16-bit")
            self.tabs.setCurrentIndex(1)
            self.load_review()
        except Exception as exc:
            self.error(str(exc))

    def load_review(self):
        if not self.review_path or self.review_future:
            return
        path, begin, span, nfft = self.review_path, self.review_start.value(), self.review_span.value(), int(self.fft.currentText())
        def work():
            with wave.open(str(path), "rb") as f:
                rate = f.getframerate()
                start = min(int(begin*rate), f.getnframes()-1)
                f.setpos(start)
                data = f.readframes(min(int(span*rate), f.getnframes()-start))
                x = np.frombuffer(data, dtype="<i2").astype(np.float32)/32768
            db, step = file_spectrogram(x, rate, nfft)
            return db, step, start/rate, len(x)/rate, rate, nfft
        self.review_load.setEnabled(False)
        self.review_future = self.background.submit(work)

    def export_review(self):
        if not self.review_path:
            self.error("Open a WAV first.")
            return
        name, _ = W.QFileDialog.getSaveFileName(self, "Audible 10× slower MP3", str(self.review_path.with_suffix(".listening_10x.mp3")), "MP3 (*.mp3)")
        if name:
            if Path(name).exists():
                self.error("Choose a new filename; existing exports are not overwritten.")
                return
            self.exports[name] = self.background.submit(export_mp3, [self.review_path], Path(name))
            self.log("Creating audible MP3 from the complete selected WAV…")

    def closeEvent(self, event):
        self.disconnect()
        if self.engine is not None:
            event.ignore()
            return
        if any(not f.done() for f in self.exports.values()):
            W.QMessageBox.information(self, "Export in progress", "Recording has stopped. An MP3 export is still running; please close the app after it finishes.")
            event.ignore()
            return
        self.timer.stop()
        if self.serial_trigger:
            self.serial_trigger.close()
        if self.software_trigger:
            self.software_trigger.close()
        self.background.shutdown(wait=True)
        event.accept()


def run():
    pg.setConfigOptions(antialias=False, imageAxisOrder="row-major")
    app = W.QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    if "--demo" in sys.argv:
        window.mode.setCurrentIndex(1)
        window.toggle_connection()
    return app.exec()
