"""Standalone cage ROI selector. Run with the project's configured Python."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
import threading
import time

import cv2
import numpy as np
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from thermal_roi import (colorize, export_rois, first_frame, load_config, make_config,
                         atomic_json, validate_cages, ExportCancelled, parse_pixels)
from gui_theme import QT_STYLE


COLORS = ['#70ef8c', '#ffd23c', '#68c8ff', '#ef91e4']


class NativeBrowser:
    """Index frame offsets lazily; only decode pixels for the selected frame."""
    def __init__(self, root, shape):
        self.path = Path(root) / 'seek_thermal.csv'
        self.shape = tuple(shape)
        self.offsets = [0]
        stat = self.path.stat()
        self.identity = (stat.st_size, stat.st_mtime_ns)

    def read(self, position, entry, cancel):
        stat = self.path.stat()
        if (stat.st_size, stat.st_mtime_ns) != self.identity:
            raise ValueError('Recording changed. Reopen it before browsing thermal frames.')
        with self.path.open('rb', buffering=8 * 1024 * 1024) as handle:
            handle.seek(self.offsets[min(position, len(self.offsets)-1)])
            index = min(position, len(self.offsets)-1)
            while index <= position:
                if cancel.is_set():
                    raise ExportCancelled('Thermal preview cancelled.')
                header = handle.readline()
                if not header:
                    raise ValueError('Thermal frame is missing.')
                metadata = dict(item.split('=', 1) for item in next(csv.reader([header.decode('utf-8-sig')])) if '=' in item)
                h, w = int(metadata['height']), int(metadata['width'])
                if (h, w) != self.shape:
                    raise ValueError('Thermal image dimensions changed.')
                lines = [handle.readline() for _ in range(h)]
                if any(not line or b'width=' in line for line in lines):
                    raise ValueError('Truncated thermal frame.')
                if index + 1 == len(self.offsets):
                    self.offsets.append(handle.tell())
                if index == position:
                    if int(metadata['timestamp_utc_ns']) != int(entry[3]):
                        raise ValueError('Thermal preview timestamp does not match the Seek log.')
                    return colorize(parse_pixels(lines, w))
                index += 1


class CageCanvas(W.QWidget):
    changed = C.Signal(int, object)
    selected = C.Signal(int)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(480, 360)
        self.image = None
        self.cages = []
        self.active = -1
        self.drawing = False
        self.drag = None
        self.proposed = None

    def set_image(self, rgb):
        rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
        self.image = G.QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], G.QImage.Format.Format_RGB888).copy()
        self.update()

    def image_rect(self):
        if self.image is None:
            return C.QRectF()
        scale = min(self.width() / self.image.width(), self.height() / self.image.height())
        w, h = self.image.width() * scale, self.image.height() * scale
        return C.QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def image_point(self, point):
        rect = self.image_rect()
        return (int(np.clip((point.x() - rect.x()) * self.image.width() / rect.width(), 0, self.image.width() - 1)),
                int(np.clip((point.y() - rect.y()) * self.image.height() / rect.height(), 0, self.image.height() - 1)))

    def mapped(self, roi):
        rect = self.image_rect()
        scale = rect.width() / self.image.width()
        x, y, w, h = roi
        return C.QRectF(rect.x() + x * scale, rect.y() + y * scale, w * scale, h * scale)

    def paintEvent(self, event):
        painter = G.QPainter(self)
        painter.fillRect(self.rect(), G.QColor('#152238'))
        if self.image is None:
            painter.setPen(G.QColor('white'))
            painter.drawText(self.rect(), C.Qt.AlignmentFlag.AlignCenter, 'Open a recording to select cages')
            return
        painter.drawImage(self.image_rect(), self.image)
        for i, cage in enumerate(self.cages):
            roi = self.proposed if i == self.active and self.proposed else cage['roi']
            rect = self.mapped(roi)
            painter.setPen(G.QPen(G.QColor(COLORS[i % len(COLORS)]), 3 if i == self.active else 2))
            painter.drawRect(rect)
            painter.drawText(rect.topLeft() + C.QPointF(4, 17), cage['mouse_id'])
            if i == self.active:
                for point in (rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()):
                    painter.drawRect(C.QRectF(point.x() - 4, point.y() - 4, 8, 8))
        if self.active < 0 and self.proposed:
            painter.setPen(G.QPen(G.QColor(COLORS[len(self.cages) % len(COLORS)]), 2))
            painter.drawRect(self.mapped(self.proposed))

    def mousePressEvent(self, event):
        if event.button() != C.Qt.MouseButton.LeftButton or self.image is None or not self.image_rect().contains(event.position()):
            return
        point = self.image_point(event.position())
        if self.drawing:
            self.drag = ('draw', point)
        else:
            if self.active >= 0:
                x, y, w, h = self.cages[self.active]['roi']
                rect = self.mapped([x, y, w, h])
                corners = [(rect.topLeft(), (x+w-1, y+h-1)), (rect.topRight(), (x, y+h-1)),
                           (rect.bottomLeft(), (x+w-1, y)), (rect.bottomRight(), (x, y))]
                for display, opposite in corners:
                    if abs(display.x() - event.position().x()) <= 9 and abs(display.y() - event.position().y()) <= 9:
                        self.drag = ('draw', opposite)
                        return
            for i in reversed(range(len(self.cages))):
                if self.mapped(self.cages[i]['roi']).contains(event.position()):
                    self.active = i
                    self.selected.emit(i)
                    self.drag = ('move', point, self.cages[i]['roi'][:])
                    self.update()
                    break

    def mouseMoveEvent(self, event):
        if not self.drag:
            return
        px, py = self.image_point(event.position())
        ax, ay = self.drag[1]
        if self.drag[0] == 'draw':
            self.proposed = [min(ax, px), min(ay, py), abs(px-ax)+1, abs(py-ay)+1]
        else:
            x, y, w, h = self.drag[2]
            self.proposed = [max(0, min(x+px-ax, self.image.width()-w)),
                             max(0, min(y+py-ay, self.image.height()-h)), w, h]
        self.update()

    def mouseReleaseEvent(self, event):
        if not self.drag:
            return
        self.mouseMoveEvent(event)
        proposal = self.proposed
        self.drag = self.proposed = None
        self.drawing = False
        self.changed.emit(self.active, proposal)
        self.update()


class Task(C.QObject):
    result = C.Signal(object)
    error = C.Signal(str)
    finished = C.Signal()
    progress = C.Signal(int, float, float)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    @C.Slot()
    def run(self):
        try:
            self.result.emit(self.fn(self))
        except ExportCancelled as exc:
            self.error.emit(str(exc) or 'Export cancelled. The partial run remains marked cancelled.')
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


def inspect_recording(root):
    frame = first_frame(root)
    previews = []
    with (Path(root) / 'seek_log.csv').open(newline='', encoding='utf-8-sig') as handle:
        for row in csv.DictReader(handle):
            previews.append((row['file'], int(row['part_frame_index']), int(row['idx']), row['sdk_utc_ns']))
    return root, frame, previews


def video_preview(root, entry, shape):
    path = (Path(root) / entry[0].replace('\\', '/')).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('Video path is outside this recording.')
    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, entry[1])
        ok, bgr = capture.read()
        if not ok or tuple(bgr.shape[:2]) != tuple(shape):
            raise ValueError('Video preview is missing or has different dimensions. Use the native thermal preview.')
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    finally:
        capture.release()


class RoiWindow(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Cage ROI · Thermal data exporter')
        self.setStyleSheet(QT_STYLE)
        self.resize(1120, 780)
        self.root = self.frame = None
        self.previews = []
        self.native_browser = None
        self.cages = []
        self.thread = self.worker = None
        self.cancel_event = threading.Event()
        self.last_export = None
        self._export_active = False
        self._export_frames = 0
        self._export_started = 0.
        self.export_timer = C.QTimer(self)
        self.export_timer.setInterval(1000)
        self.export_timer.timeout.connect(self.update_running_status)
        widget = W.QWidget()
        layout = W.QVBoxLayout(widget)
        self.setCentralWidget(widget)
        self.controls = W.QWidget()
        top = W.QHBoxLayout(self.controls)
        for text, fn in [('Open recording…', self.open_recording), ('Load cages…', self.load_cages),
                         ('Save cages…', self.save_cages)]:
            button = W.QPushButton(text)
            button.clicked.connect(fn)
            top.addWidget(button)
        layout.addWidget(self.controls)
        self.source_label = W.QLabel('Select a finished recording folder with Seek thermal data.')
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        self.body = W.QWidget()
        row = W.QHBoxLayout(self.body)
        self.canvas = CageCanvas()
        self.canvas.changed.connect(self.change_roi)
        self.canvas.selected.connect(lambda i: self.cage_list.setCurrentRow(i))
        row.addWidget(self.canvas, 1)
        panel = W.QWidget()
        panel.setMaximumWidth(300)
        side = W.QVBoxLayout(panel)
        instructions = W.QLabel('Drag a rectangle around each cage. Drag inside to move; drag corners to resize.')
        instructions.setWordWrap(True)
        side.addWidget(instructions)
        self.cage_list = W.QListWidget()
        self.cage_list.currentRowChanged.connect(self.select_cage)
        side.addWidget(self.cage_list)
        for text, fn in [('Add cage', self.add_cage), ('Rename / genotype', self.rename_cage),
                         ('Redraw selected', self.redraw), ('Delete selected', self.delete_cage)]:
            button = W.QPushButton(text)
            button.clicked.connect(fn)
            side.addWidget(button)
        self.crop = W.QLabel('Selected cage preview')
        self.crop.setMinimumSize(180, 160)
        self.crop.setAlignment(C.Qt.AlignmentFlag.AlignCenter)
        side.addWidget(self.crop)
        self.crop_caption = W.QLabel('Cage preview (scaled to fit)')
        self.crop_caption.setWordWrap(True)
        side.addWidget(self.crop_caption)
        enlarge = W.QPushButton('Enlarge cage preview')
        enlarge.clicked.connect(self.enlarge_crop)
        side.addWidget(enlarge)
        self.review = W.QCheckBox('Cage boundaries checked\nfor this recording')
        self.review.setChecked(False)
        self.review.setStyleSheet('QCheckBox::indicator { width:18px; height:18px; border:2px solid #a5b4c7; border-radius:3px; } '
                                 'QCheckBox::indicator:checked { background:#3fb950; border-color:#3fb950; }')
        self.review.toggled.connect(self.update_export_readiness)
        side.addWidget(self.review)
        sidebar = W.QScrollArea()
        sidebar.setWidgetResizable(True)
        sidebar.setMinimumWidth(280)
        sidebar.setMaximumWidth(320)
        sidebar.setWidget(panel)
        row.addWidget(sidebar)
        layout.addWidget(self.body, 1)
        self.browse_controls = W.QWidget()
        browser = W.QHBoxLayout(self.browse_controls)
        native = W.QPushButton('Native thermal frame')
        native.clicked.connect(self.native_preview)
        browser.addWidget(native)
        self.slider = W.QSlider(C.Qt.Orientation.Horizontal)
        self.slider.sliderReleased.connect(self.browse_video)
        browser.addWidget(self.slider, 1)
        preview_button = W.QPushButton('Preview selected time')
        preview_button.clicked.connect(self.browse_video)
        browser.addWidget(preview_button)
        layout.addWidget(self.browse_controls)
        self.preview_label = W.QLabel('Video is for checking cage positions; export uses thermal pixels.')
        layout.addWidget(self.preview_label)
        self.export_status = W.QLabel()
        self.export_status.setWordWrap(True)
        self.export_status.setMinimumHeight(44)
        layout.addWidget(self.export_status)
        actions = W.QHBoxLayout()
        self.export_button = W.QPushButton('Export pixels + temperatures…')
        self.export_button.clicked.connect(self.export)
        actions.addWidget(self.export_button)
        self.cancel_button = W.QPushButton('Cancel export')
        self.cancel_button.clicked.connect(self.cancel_export)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.cancel_button)
        open_result = W.QPushButton('Open last export')
        open_result.clicked.connect(self.open_result)
        actions.addWidget(open_result)
        layout.addLayout(actions)
        self.progress_bar = W.QProgressBar()
        self.progress_bar.setMinimumHeight(22)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat('Not running')
        layout.addWidget(self.progress_bar)
        self.status = W.QLabel('Ready')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.update_export_readiness()

    def message(self, text):
        self.status.setText(text)

    def set_export_status(self, state, text):
        colors = {'waiting': '#e3a008', 'ready': '#4c9aff', 'running': '#4c9aff',
                  'complete': '#3fb950', 'error': '#ef5b60', 'cancelled': '#e3a008'}
        color = colors[state]
        self.export_status.setStyleSheet(f'QLabel {{ background:#1f2630; color:{color}; '
                                        f'border:1px solid {color}; border-radius:5px; padding:8px; font-weight:600; }}')
        self.export_status.setText(text)

    def update_export_readiness(self):
        if not hasattr(self, 'export_status') or self._export_active:
            return
        if self.frame is None:
            text = 'Export not running — open a recording first.'
        elif not self.cages:
            text = 'Export not running — add a cage and draw its rectangle.'
        elif not self.review.isChecked():
            text = 'Export not running — check “Cage boundaries checked for this recording” on the right, then press Export.'
        else:
            self.set_export_status('ready', 'Ready to export — press Export and choose an output folder to start.')
            return
        self.set_export_status('waiting', text)

    def update_running_status(self):
        if not self._export_active:
            return
        elapsed = max(0, int(time.monotonic() - self._export_started))
        clock = f'{elapsed // 60}:{elapsed % 60:02d}'
        if self.cancel_event.is_set():
            text = f'Stopping export — finishing the current operation safely · elapsed {clock}'
        else:
            text = f'EXPORT RUNNING — {self._export_frames:,} frames processed · elapsed {clock}'
        self.set_export_status('running', text)

    def cancel_export(self):
        self.cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText('Stopping…')
        self.update_running_status()

    def task_error(self, text):
        self.message(text)
        if self._export_active:
            self.export_timer.stop()
            cancelled = self.cancel_event.is_set()
            self.set_export_status('cancelled' if cancelled else 'error',
                                   ('Export cancelled — ' if cancelled else 'Export failed — ') + text)
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setFormat('Cancelled' if cancelled else 'Failed')

    def run_task(self, fn, callback, exporting=False):
        if self.thread is not None:
            return
        self.controls.setEnabled(False)
        self.body.setEnabled(False)
        self.browse_controls.setEnabled(False)
        self.export_button.setEnabled(False)
        self.cancel_button.setEnabled(exporting)
        if exporting:
            self._export_active = True
            self._export_started = time.monotonic()
            self._export_frames = 0
            self.export_button.setText('Export running…')
            self.cancel_button.setText('Cancel export')
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat('Starting export…')
            self.update_running_status()
            self.export_timer.start()
        self.thread = C.QThread(self)
        self.worker = Task(fn)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.result.connect(callback)
        self.worker.error.connect(self.task_error)
        self.worker.progress.connect(self.show_progress)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.task_done)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def task_done(self):
        self.export_timer.stop()
        self._export_active = False
        self.thread = self.worker = None
        self.controls.setEnabled(True)
        self.body.setEnabled(True)
        self.browse_controls.setEnabled(True)
        self.export_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText('Cancel export')
        self.export_button.setText('Export pixels + temperatures…')

    def open_recording(self):
        root = W.QFileDialog.getExistingDirectory(self, 'Select recording folder')
        if root:
            self.message('Reading first thermal frame and video index…')
            self.run_task(lambda _: inspect_recording(root), self.recording_loaded)

    def recording_loaded(self, result):
        self.root, self.frame, self.previews = result
        self.native_browser = NativeBrowser(self.root, self.frame.pixels.shape)
        self.cages = []
        self.slider.setRange(0, max(0, len(self.previews) - 1))
        self.slider.setValue(0)
        self.review.setChecked(False)
        self.source_label.setText(str(self.root))
        self.refresh_cages()
        self.canvas.set_image(colorize(self.frame.pixels))
        self.preview_label.setText(f'Native thermal frame {self.frame.index} · calibrated Celsius')
        self.message('Add a cage and drag its rectangle. Default names are WT_01, WT_02, …')

    def native_preview(self):
        if self.frame is None or self.thread is not None:
            return
        position = self.slider.value()
        entry = self.previews[position]
        self.cancel_event.clear()
        self.message('Loading selected thermal frame. The first jump forward indexes the file; later visits are faster.')
        self.run_task(lambda _: self.native_browser.read(position, entry, self.cancel_event), self.native_loaded)

    def native_loaded(self, rgb):
        entry = self.previews[self.slider.value()]
        self.preview_loaded(rgb)
        self.preview_label.setText(f'Native thermal frame {entry[2]} · UTC ns {entry[3]} · calibrated Celsius')
        self.message('Selected native thermal frame loaded.')

    def browse_video(self):
        if self.frame is None or not self.previews:
            return
        entry = self.previews[self.slider.value()]
        self.run_task(lambda _: video_preview(self.root, entry, self.frame.pixels.shape), self.preview_loaded)
        self.preview_label.setText(f'Video frame {entry[2]} · UTC ns {entry[3]} · position check only')

    def preview_loaded(self, rgb):
        self.canvas.set_image(rgb)
        self.update_crop()

    def refresh_cages(self, selected=-1):
        self.canvas.cages = self.cages
        self.cage_list.clear()
        for cage in self.cages:
            self.cage_list.addItem(f"{cage['mouse_id']} · {cage['genotype']} · {cage['roi']}")
        self.cage_list.setCurrentRow(selected)
        self.canvas.update()
        self.update_export_readiness()

    def select_cage(self, i):
        self.canvas.active = i
        self.canvas.drawing = False
        self.canvas.update()
        self.update_crop()

    def update_crop(self):
        i = self.canvas.active
        if i < 0 or self.canvas.image is None:
            self.crop.setText('Select a cage to preview')
            self.crop_caption.setText('Cage preview (scaled to fit)')
            return
        x, y, w, h = self.cages[i]['roi']
        pixmap = G.QPixmap.fromImage(self.canvas.image.copy(x, y, w, h))
        self.crop.setPixmap(pixmap.scaled(260, 180, C.Qt.AspectRatioMode.KeepAspectRatio))
        self.crop_caption.setText(f'{w} × {h} native pixels · full ROI, scaled to fit')

    def enlarge_crop(self):
        i = self.canvas.active
        if i < 0 or self.canvas.image is None:
            return
        x, y, w, h = self.cages[i]['roi']
        dialog = W.QDialog(self)
        dialog.setWindowTitle(f"{self.cages[i]['mouse_id']} — full cage ROI ({w} × {h} pixels)")
        dialog.resize(650, 700)
        layout = W.QVBoxLayout(dialog)
        view = CageCanvas()
        view.image = self.canvas.image.copy(x, y, w, h)
        layout.addWidget(view)
        layout.addWidget(W.QLabel('Entire selected ROI. Resize this window to enlarge; proportions are preserved.'))
        dialog.exec()

    def add_cage(self):
        if self.frame is None:
            return
        self.cage_list.setCurrentRow(-1)
        self.canvas.active = -1
        self.canvas.drawing = True
        self.message('Drag over the new cage, then release.')

    def redraw(self):
        if self.canvas.active >= 0:
            self.canvas.drawing = True
            self.message('Drag the replacement rectangle.')

    def change_roi(self, i, roi):
        cages = json.loads(json.dumps(self.cages))
        if i < 0:
            number = 1
            while any(c['mouse_id'] == f'WT_{number:02d}' for c in cages):
                number += 1
            cages.append({'mouse_id': f'WT_{number:02d}', 'genotype': 'WT', 'roi': roi})
            i = len(cages) - 1
        else:
            cages[i]['roi'] = roi
        try:
            validate_cages(cages, self.frame.pixels.shape)
        except ValueError as exc:
            self.message(str(exc))
            return
        self.cages = cages
        self.review.setChecked(False)
        self.refresh_cages(i)
        self.message('Cage saved. Check the boundary, then export or add another cage.')

    def rename_cage(self):
        i = self.canvas.active
        if i < 0:
            return
        name, ok = W.QInputDialog.getText(self, 'Mouse ID', 'Unique mouse ID:', text=self.cages[i]['mouse_id'])
        if not ok:
            return
        genotype, ok = W.QInputDialog.getText(self, 'Genotype', 'Genotype:', text=self.cages[i]['genotype'])
        if not ok:
            return
        cages = json.loads(json.dumps(self.cages))
        cages[i].update(mouse_id=name.strip(), genotype=genotype.strip())
        try:
            validate_cages(cages, self.frame.pixels.shape)
        except ValueError as exc:
            self.message(str(exc))
            return
        self.cages = cages
        self.refresh_cages(i)

    def delete_cage(self):
        i = self.canvas.active
        if i >= 0:
            self.cages.pop(i)
            self.review.setChecked(False)
            self.refresh_cages()

    def save_cages(self):
        if self.frame is None:
            return
        try:
            config = make_config(self.root, self.frame.pixels.shape, self.cages)
            path, _ = W.QFileDialog.getSaveFileName(self, 'Save cage selections', 'roi_config.json', 'JSON (*.json)')
            if path:
                atomic_json(path, config)
                self.message('Cage configuration saved.')
        except (ValueError, OSError) as exc:
            self.message(str(exc))

    def load_cages(self):
        if self.frame is None:
            self.message('Open a recording before loading cage selections.')
            return
        path, _ = W.QFileDialog.getOpenFileName(self, 'Load cage selections', '', 'JSON (*.json)')
        if not path:
            return
        try:
            config, changed = load_config(path, self.root, self.frame.pixels.shape)
            self.cages = config['cages']
            self.review.setChecked(False)
            self.refresh_cages(0)
            self.message(('Different source recording. ' if changed else '') + 'Review the overlaid cage boundaries before exporting.')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.message(str(exc))

    def export(self):
        if self.thread is not None:
            return
        if self.frame is None:
            self.update_export_readiness()
            return
        if not self.review.isChecked():
            self.message('Review the cage overlays and check “Cage boundaries checked for this recording”.')
            self.update_export_readiness()
            self.review.setFocus()
            return
        try:
            config = make_config(self.root, self.frame.pixels.shape, self.cages)
        except ValueError as exc:
            self.message(str(exc))
            self.set_export_status('error', f'Export not started — {exc}')
            return
        self.set_export_status('waiting', 'Export not started yet — choose the output folder in the folder dialog.')
        parent = W.QFileDialog.getExistingDirectory(self, 'Choose export parent folder (a new run folder will be created)')
        if not parent:
            self.set_export_status('waiting', 'Export not started — no output folder selected. Press Export to try again.')
            return
        self.cancel_event.clear()
        self.progress_bar.setValue(0)
        self.message('Exporting all saved frames for all cages…')

        def job(worker):
            last = [0.]
            def report(count, fraction, eta):
                now = time.monotonic()
                if now - last[0] >= .25:
                    worker.progress.emit(count, fraction, eta)
                    last[0] = now
            return export_rois(self.root, config, parent, self.cancel_event, report)
        self.run_task(job, self.export_finished, exporting=True)

    def show_progress(self, count, fraction, eta):
        self._export_frames = count
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(int(fraction * 1000))
        self.progress_bar.setFormat(f'{fraction:.1%} exported')
        self.update_running_status()
        self.message(f'{count:,} frames · {fraction:.1%} · approximately {eta / 60:.1f} minutes remaining')

    def export_finished(self, out):
        self.export_timer.stop()
        self.last_export = out
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(1000)
        self.progress_bar.setFormat('Complete — 100%')
        self.set_export_status('complete', 'EXPORT COMPLETE — click “Open last export” to view the saved files.')
        self.message(f'Export complete: {out}')

    def open_result(self):
        if self.last_export:
            G.QDesktopServices.openUrl(C.QUrl.fromLocalFile(str(self.last_export)))

    def closeEvent(self, event):
        if self.thread is not None:
            self.cancel_export()
            self.message('Waiting for the current operation to finish safely. Close again when it finishes.')
            event.ignore()
        else:
            event.accept()


def main():
    app = W.QApplication(sys.argv)
    window = RoiWindow()
    window.show()
    if len(sys.argv) > 1:
        root = sys.argv[1]
        window.run_task(lambda _: inspect_recording(root), window.recording_loaded)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
