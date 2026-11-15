"""Read native thermal frames and indexed Seek/FLIR video previews."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import re

import cv2
import numpy as np

from .exporter import ExportCancelled, colorize, first_frame, parse_pixels


@dataclass
class VideoFrame:
    index: int
    pixels: np.ndarray  # RGB uint8 array (H, W, 3)
    metadata: dict


@dataclass
class StreamInfo:
    key: str                # e.g. 'seek', 'flir_cam0'
    label: str              # e.g. 'Seek Thermal', 'FLIR Camera 0'
    modality: str           # 'thermal' or 'video'
    shape: tuple[int, int]  # (height, width)
    first_frame: object     # Frame or VideoFrame
    has_video: bool
    previews: list          # [(video_file, part_frame_index, frame_index, timestamp), ...]
    log_file: str | None    # relative filename of CSV log
    video_files: list[str]  # relative paths of video files


class InspectionResult(tuple):
    """3-tuple (root, frame, previews) with rich stream metadata for multi-stream support."""
    root: Path
    frame: object
    previews: list
    streams: dict[str, StreamInfo]
    active_stream: str
    has_seek: bool
    has_flir: bool
    has_video: bool

    def __new__(cls, root, frame, previews, streams, active_stream):
        instance = super().__new__(cls, (root, frame, previews))
        instance.root = Path(root)
        instance.frame = frame
        instance.previews = previews
        instance.streams = streams
        instance.active_stream = active_stream
        instance.has_seek = 'seek' in streams
        instance.has_flir = any(k.startswith('flir') for k in streams)
        active_info = streams.get(active_stream)
        instance.has_video = active_info.has_video if active_info else False
        return instance


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


def _inspect_flir_stream(root: Path, cam_index: int) -> StreamInfo | None:
    """Detect and index a FLIR camera stream (from log CSV or video files)."""
    log_name = f'flir_log_cam{cam_index}.csv'
    log_path = root / log_name
    previews = []
    video_files = []
    first_rgb = None
    shape = None

    # Quick check: are there candidate FLIR video files on disk?
    disk_mp4s = sorted(list(root.glob(f'flir_cam{cam_index}*.mp4')) +
                       list((root / 'video_parts').glob(f'flir_cam{cam_index}*.mp4')) +
                       (list(root.glob('flir*.mp4')) if cam_index == 0 else []) +
                       (list((root / 'video_parts').glob('flir*.mp4')) if cam_index == 0 else []))

    if log_path.is_file():
        # Check if log actually references any existing video file before full parse
        has_accessible_video = bool(disk_mp4s)
        if not has_accessible_video:
            with log_path.open(newline='', encoding='utf-8-sig') as handle:
                reader = csv.DictReader(handle)
                for i, row in enumerate(reader):
                    vfile = row.get('video_file') or row.get('file')
                    if vfile and (root / vfile.replace('\\', '/')).is_file():
                        has_accessible_video = True
                        break
                    if i > 25:
                        break
        if not has_accessible_video:
            return None

        checked_files = {}
        with log_path.open(newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                vfile = row.get('video_file') or row.get('file')
                if not vfile:
                    vfile = f'flir_cam{cam_index}.mp4'
                vfile = vfile.replace('\\', '/')
                if vfile not in checked_files:
                    is_f = (root / vfile).is_file()
                    checked_files[vfile] = is_f
                    if is_f and vfile not in video_files:
                        video_files.append(vfile)
                part_idx = int(row.get('part_frame_index', row.get('idx', 0)))
                frame_idx = int(row.get('idx', len(previews)))
                ts = row.get('cam_ts_ns') or row.get('pc_time') or str(frame_idx)
                previews.append((vfile, part_idx, frame_idx, ts))

    # If log didn't list existing files or wasn't present, search disk for FLIR MP4s
    if not video_files:
        for cand in disk_mp4s:
            rel = str(cand.relative_to(root)).replace('\\', '/')
            if rel not in video_files:
                video_files.append(rel)

    if not video_files:
        return None

    # Read first frame and dimensions from the first video file
    first_video_path = root / video_files[0]
    cap = cv2.VideoCapture(str(first_video_path))
    try:
        ok, bgr = cap.read()
        if not ok or bgr is None:
            return None
        shape = bgr.shape[:2]
        first_rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    finally:
        cap.release()

    # If previews were not constructed from log CSV, construct them from video files
    if not previews:
        total_idx = 0
        for rel in video_files:
            vpath = root / rel
            cap = cv2.VideoCapture(str(vpath))
            try:
                part_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                if part_count <= 0:
                    part_count = 1  # Fallback if container doesn't report frame count
                for part_idx in range(part_count):
                    previews.append((rel, part_idx, total_idx, str(total_idx)))
                    total_idx += 1
            finally:
                cap.release()

    first_frame = VideoFrame(index=0, pixels=first_rgb, metadata={'camera': f'flir_cam{cam_index}', 'shape': shape})
    return StreamInfo(
        key=f'flir_cam{cam_index}',
        label=f'FLIR Camera {cam_index}',
        modality='video',
        shape=shape,
        first_frame=first_frame,
        has_video=True,
        previews=previews,
        log_file=log_name if log_path.is_file() else None,
        video_files=video_files,
    )


def inspect_recording(root):
    """Inspect a recording folder and return root, frame, previews (and available streams)."""
    root = Path(root).resolve()
    streams: dict[str, StreamInfo] = {}

    # 1. Check for Seek thermal stream
    seek_thermal = root / 'seek_thermal.csv'
    seek_log = root / 'seek_log.csv'
    if seek_thermal.is_file() and seek_log.is_file():
        frame = first_frame(root)
        previews = []
        video_files = []
        checked_files = {}
        with seek_log.open(newline='', encoding='utf-8-sig') as handle:
            for row in csv.DictReader(handle):
                vfile = row['file'].replace('\\', '/')
                if vfile not in checked_files:
                    is_f = (root / vfile).is_file()
                    checked_files[vfile] = is_f
                    if is_f and vfile not in video_files:
                        video_files.append(vfile)
                previews.append((vfile, int(row['part_frame_index']), int(row['idx']), row['sdk_utc_ns']))
        has_video = bool(video_files) and all(checked_files.values())
        streams['seek'] = StreamInfo(
            key='seek',
            label='Seek Thermal',
            modality='thermal',
            shape=frame.pixels.shape[:2],
            first_frame=frame,
            has_video=has_video,
            previews=previews,
            log_file='seek_log.csv',
            video_files=video_files,
        )

    # 2. Check for FLIR camera streams (cam0, cam1, ...)
    flir_indices = set()
    for log_file in root.glob('flir_log_cam*.csv'):
        m = re.search(r'cam(\d+)', log_file.name)
        if m:
            flir_indices.add(int(m.group(1)))
    for mp4_file in list(root.glob('flir_cam*.mp4')) + list((root / 'video_parts').glob('flir_cam*.mp4')):
        m = re.search(r'cam(\d+)', mp4_file.name)
        if m:
            flir_indices.add(int(m.group(1)))
    if not flir_indices and (root.glob('flir*.mp4') or (root / 'video_parts').glob('flir*.mp4')):
        flir_indices.add(0)

    for idx in sorted(flir_indices):
        info = _inspect_flir_stream(root, idx)
        if info:
            streams[info.key] = info

    if not streams:
        raise ValueError('Recording folder contains neither Seek thermal data nor playable FLIR video files.')

    # Default to 'seek' if present, otherwise first available FLIR stream
    active_key = 'seek' if 'seek' in streams else next(iter(streams))
    active_stream = streams[active_key]

    return InspectionResult(root, active_stream.first_frame, active_stream.previews, streams, active_key)


def video_preview(root, entry, shape):
    """Read a preview frame from a video file at the given part frame index."""
    path = (Path(root) / entry[0].replace('\\', '/')).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('Video path is outside this recording.')
    if not path.is_file():
        raise ValueError(f'Video file does not exist: {path.name}')
    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, entry[1])
        ok, bgr = capture.read()
        if not ok or tuple(bgr.shape[:2]) != tuple(shape[:2]):
            raise ValueError('Video preview is missing or has different dimensions. Use the native thermal preview.')
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    finally:
        capture.release()
