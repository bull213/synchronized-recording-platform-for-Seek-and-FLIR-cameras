"""Qt worker for cancellable cage preview and export tasks."""
from PySide6 import QtCore as C

from .exporter import ExportCancelled


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
