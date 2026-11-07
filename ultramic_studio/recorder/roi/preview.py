"""Read native thermal frames and indexed Seek video previews."""
import csv
from pathlib import Path

import cv2

from .exporter import ExportCancelled, colorize, first_frame, parse_pixels


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
