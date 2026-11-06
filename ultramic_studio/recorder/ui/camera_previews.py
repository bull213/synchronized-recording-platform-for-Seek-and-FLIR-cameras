"""Camera-preview construction, reflow, and rendering for the recorder.

The window remains the owner of widgets, timers, and camera state.
"""
import cv2
from PySide6 import QtCore, QtGui, QtWidgets as W

from ultramic_studio.gui import button, label


def add_camera_panel(window, key, title, index):
    group = W.QGroupBox(title)
    layout = W.QVBoxLayout(group)
    device_status = label('Waiting for frames…', True)
    device_status.setWordWrap(True)
    layout.addWidget(device_status)
    window._device_labels[key] = device_status
    preview = W.QLabel('Waiting for frames…')
    preview.setAccessibleName(f'{title} preview')
    preview.setAccessibleDescription('Live camera image; use the adjacent button to enlarge it.')
    preview.setObjectName('preview')
    preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    preview.setMinimumSize(120, 48)
    preview.setSizePolicy(W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Ignored)
    layout.addWidget(preview, 1)
    enlarge = button('Enlarge', lambda checked=False, k=key: window._enlarge_camera(k))
    enlarge.setAccessibleName(f'Enlarge {title} preview')
    layout.addWidget(enlarge)
    preview.setToolTip('Double-click to enlarge this camera or return to all cameras.')
    preview.installEventFilter(window)
    group.setParent(window.preview_widget)
    window._camera_panels[key] = group
    window._enlarge_buttons[key] = enlarge
    window._preview_labels[key] = preview
    window._reflow_timer.start(0)


def reflow_previews(window):
    width = window.preview_scroll.viewport().width()
    # Three panels fit a compact laptop once settings are hidden. Avoid
    # wasting scarce vertical space on a second row in that view.
    columns = 3 if width >= 780 else 2 if width >= 520 else 1
    if window.height() < 620 and width >= 500:
        columns = 3
    keys = [window._focused_camera] if window._focused_camera else list(window._camera_panels)
    signature = (columns, tuple(keys))
    if signature == window._layout_signature:
        return
    window._layout_signature = signature
    # Keep live preview widgets; only rearrange their splitter containers.
    for panel in window._camera_panels.values():
        panel.hide()
        panel.setParent(window.preview_scroll)
    for row in window._preview_rows:
        row.setParent(None)
        row.deleteLater()
    window._preview_rows.clear()
    for index, key in enumerate(keys):
        if index % columns == 0:
            row = W.QSplitter(QtCore.Qt.Orientation.Horizontal)
            row.setChildrenCollapsible(False)
            row.setHandleWidth(8)
            window.preview_widget.addWidget(row)
            window._preview_rows.append(row)
        row.addWidget(window._camera_panels[key])
        window._camera_panels[key].show()
        window._enlarge_buttons[key].setText('All cameras' if window._focused_camera else 'Enlarge')
        window._enlarge_buttons[key].setAccessibleName(
            'Show all cameras' if window._focused_camera else f'Enlarge {window._camera_panels[key].title()} preview')
    window.preview_widget.setMinimumHeight(200 * len(window._preview_rows))
    for row in window._preview_rows:
        row.setSizes([1000] * row.count())
    window.preview_widget.setSizes([1000] * len(window._preview_rows))


def render_camera_previews(window):
    if not window.preview_scroll.isVisible():
        return
    with window.core.latest_ir_lock:
        snap = window.core.latest_ir
    frames = {'seek': cv2.cvtColor(snap[1], cv2.COLOR_BGRA2BGR)} if snap else {}
    with window.core.flir_preview_lock:
        frames.update({f'flir_{idx}': frame.copy() for idx, frame in window.core.flir_previews.items()})
    for key, frame in frames.items():
        preview = window._preview_labels.get(key)
        if preview is None or not preview.isVisible():
            continue
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        image = QtGui.QImage(rgb.data, width, height, rgb.strides[0], QtGui.QImage.Format.Format_RGB888).copy()
        pixmap = QtGui.QPixmap.fromImage(image).scaled(preview.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                                     QtCore.Qt.TransformationMode.SmoothTransformation)
        preview.setPixmap(pixmap)
