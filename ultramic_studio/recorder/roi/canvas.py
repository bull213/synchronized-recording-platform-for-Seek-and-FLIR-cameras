"""Native-coordinate drawing and selection of cage rectangles."""
import numpy as np
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W


COLORS = ['#70ef8c', '#ffd23c', '#68c8ff', '#ef91e4']


class CageCanvas(W.QWidget):
    changed = C.Signal(int, object)
    selected = C.Signal(int)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(240, 180)
        self.setAccessibleName('Cage selection preview')
        self.setAccessibleDescription('Draw cages with the mouse, or use the cage list and coordinate fields.')
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
