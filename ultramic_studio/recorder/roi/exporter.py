"""Native-pixel cage selection configuration and bounded-memory thermal exports."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import threading
import time
import uuid

import numpy as np

from .storage import COMPRESSION_LEVEL, MAX_ARCHIVE_WORKERS, RoiChunkWriter


class ExportCancelled(Exception):
    pass


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def source_identity(root):
    root = Path(root).resolve()
    return {'directory': str(root), 'files': {
        name: {'size': (root / name).stat().st_size, 'mtime_ns': (root / name).stat().st_mtime_ns}
        for name in ('seek_thermal.csv', 'seek_log.csv')}}


def validate_cages(cages, shape):
    if not cages:
        raise ValueError('Add at least one cage.')
    height, width = shape
    names, boxes = set(), []
    for cage in cages:
        name = cage['mouse_id']
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name)
                or PureWindowsPath(name).is_reserved() or name.casefold() in names):
            raise ValueError('Mouse IDs must be unique safe names: letters, numbers, underscores or hyphens.')
        if not isinstance(cage.get('genotype'), str) or not cage['genotype'].strip():
            raise ValueError('Each cage needs a genotype label.')
        roi = cage['roi']
        if (len(roi) != 4 or any(type(v) is not int for v in roi)
                or min(roi[:2]) < 0 or min(roi[2:]) < 1):
            raise ValueError('A cage must have integer x, y, width and height, with positive dimensions.')
        x, y, w, h = roi
        if x + w > width or y + h > height:
            raise ValueError(f'{name}: cage extends outside the thermal image.')
        for a, b, c, d in boxes:
            if max(x, a) < min(x + w, a + c) and max(y, b) < min(y + h, b + d):
                raise ValueError('Cage regions overlap. Give each mouse a separate region.')
        names.add(name.casefold())
        boxes.append(roi)


def make_config(root, shape, cages):
    validate_cages(cages, shape)
    return {'schema_version': 1, 'source': source_identity(root), 'shape': list(shape),
            'coordinates': 'native [x,y,width,height]; pixels[y:y+height,x:x+width]',
            'cages': json.loads(json.dumps(cages))}


def load_config(path, root, shape):
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if config.get('schema_version') != 1 or config.get('shape') != list(shape):
        raise ValueError('Unsupported configuration or different thermal image dimensions.')
    validate_cages(config['cages'], shape)
    return config, config.get('source') != source_identity(root)


@dataclass
class Frame:
    index: int
    take_id: int
    utc_ns: int
    host_ns: int
    pixels: np.ndarray
    metadata: dict
    bytes_read: int


def parse_pixels(lines, width):
    # Fast path for the recorder's common NN.N, format, validating every byte.
    rows = [line.rstrip(b'\r\n') for line in lines]
    if all(len(row) == width * 5 for row in rows):
        raw = np.frombuffer(b''.join(rows), dtype=np.uint8).reshape(len(rows), width, 5)
        digits = raw[:, :, [0, 1, 3]]
        if ((digits >= 48).all() and (digits <= 57).all()
                and (raw[:, :, 2] == 46).all() and (raw[:, :, 4] == 44).all()):
            return ((raw[:, :, 0].astype(np.int16) - 48) * 100
                    + (raw[:, :, 1].astype(np.int16) - 48) * 10
                    + raw[:, :, 3].astype(np.int16) - 48).astype(np.float64) / 10
    pixels = np.empty((len(rows), width), dtype=np.float64)
    for y, row in enumerate(rows):
        cells = row.removesuffix(b',').split(b',')
        if len(cells) != width:
            raise ValueError('Malformed thermal pixel row.')
        pixels[y] = cells
    return pixels


def iter_frames(root, cancel=None, digest=None):
    root = Path(root)
    previous = (-1, -1, 0)
    shape = None
    with (root / 'seek_thermal.csv').open('rb', buffering=8 * 1024 * 1024) as raw, \
            (root / 'seek_log.csv').open(newline='', encoding='utf-8-sig') as log:
        rows = csv.DictReader(log)
        while header := raw.readline():
            if cancel is not None and cancel.is_set():
                raise ExportCancelled('Export cancelled.')
            if digest is not None:
                digest.update(header)
            metadata = {}
            for item in next(csv.reader([header.decode('utf-8-sig')])):
                if '=' in item:
                    key, value = item.split('=', 1)
                    if key in metadata and metadata[key] != value:
                        raise ValueError(f'Conflicting frame metadata: {key}')
                    metadata[key] = value
            h, w = int(metadata['height']), int(metadata['width'])
            if not (0 < h <= 4096 and 0 < w <= 4096):
                raise ValueError('Invalid frame dimensions.')
            if shape is not None and shape != (h, w):
                raise ValueError('Thermal resolution changed; redefine cage selections.')
            shape = h, w
            if tuple(int(metadata[k]) for k in ('type', 'channels', 'pixel_depth')) != (16, 1, 32):
                raise ValueError('Expected native single-channel Celsius thermal pixels.')
            row = next(rows, None)
            if row is None:
                raise ValueError('Thermal frames outnumber Seek log rows.')
            index, take, utc, host = (int(row[k]) for k in
                                    ('idx', 'take_id', 'sdk_utc_ns', 'host_received_monotonic_ns'))
            if (take, utc, host) != tuple(int(metadata[k]) for k in
                    ('take_id', 'timestamp_utc_ns', 'host_received_monotonic_ns')):
                raise ValueError(f'Thermal frame {index} does not match the Seek log.')
            if index <= previous[0] or host <= previous[1] or take < previous[2] or min(take, utc, host) <= 0:
                raise ValueError('Frame IDs and acquisition times must increase; takes must remain ordered.')
            if row.get('calibration') != metadata.get('calibration'):
                raise ValueError('Thermal calibration does not match the Seek log.')
            previous = index, host, take
            lines = [raw.readline() for _ in range(h)]
            if any(not line or b'width=' in line for line in lines):
                raise ValueError(f'Truncated thermal frame {index}.')
            if digest is not None:
                for line in lines:
                    digest.update(line)
            yield Frame(index, take, utc, host, parse_pixels(lines, w), metadata, raw.tell())
        if next(rows, None) is not None:
            raise ValueError('Seek log has more rows than thermal frames.')
        if shape is None:
            raise ValueError('Recording has no thermal frames.')


def first_frame(root):
    frames = iter_frames(root)
    try:
        return next(frames)
    finally:
        frames.close()


def temperatures(crop):
    valid = np.isfinite(crop)
    if not valid.all():
        return [None, None, None, int(valid.sum()), 'nonfinite_pixels']
    threshold = np.percentile(crop, 95)
    return [float(crop.max()), float(crop.mean()), float(crop[crop >= threshold].mean()), crop.size, 'ok']


def colorize(pixels):
    import cv2
    valid = np.isfinite(pixels)
    lo, hi = np.percentile(pixels[valid], [2, 98]) if valid.any() else (0, 1)
    gray = np.nan_to_num(np.clip((pixels - lo) / max(hi - lo, .1), 0, 1), nan=0, posinf=0, neginf=0)
    return cv2.cvtColor(cv2.applyColorMap((gray * 255).astype('uint8'), cv2.COLORMAP_INFERNO), cv2.COLOR_BGR2RGB)


def save_overview(path, pixels, cages):
    import cv2
    image = cv2.cvtColor(colorize(pixels), cv2.COLOR_RGB2BGR)
    image = cv2.resize(image, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    for i, cage in enumerate(cages):
        x, y, w, h = cage['roi']
        color = [(140, 240, 70), (60, 210, 255), (255, 170, 100)][i % 3]
        cv2.rectangle(image, (x * 3, y * 3), ((x + w) * 3 - 1, (y + h) * 3 - 1), color, 2)
        cv2.putText(image, cage['mouse_id'], (x * 3 + 3, y * 3 + 18), cv2.FONT_HERSHEY_SIMPLEX, .5, color, 1)
    success, encoded = cv2.imencode('.png', image)
    if not success:
        raise OSError('Could not encode ROI overview.')
    Path(path).write_bytes(encoded.tobytes())


def export_rois(root, config, output_parent, cancel=None, progress=None, max_frames=None, *, include_video=False):
    """Export all frames by default; max_frames is an explicitly marked sample export."""
    root, output_parent = Path(root).resolve(), Path(output_parent).resolve()
    cancel = cancel or threading.Event()
    shape, cages = config['shape'], config['cages']
    validate_cages(cages, shape)
    identity = source_identity(root)
    if config['source'] != identity:
        raise ValueError('Source recording changed. Review and save cage selections again.')
    if max_frames is not None and max_frames < 1:
        raise ValueError('Sample frame limit must be positive.')
    output_parent.mkdir(parents=True, exist_ok=True)
    out = output_parent / (datetime.now().strftime('roi_%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8])
    out.mkdir()
    manifest = {'status': 'incomplete', 'source': identity, 'config': config,
                'scope': 'all_frames' if max_frames is None else 'sample', 'sample_limit': max_frames,
                'calibration': 'Stored calibrated Celsius values preserved; no recalibration',
                'primary_metric': 'ROI maximum; warmest_5pct_mean includes ties at the 95th percentile',
                'frames': 0, 'calibration_profiles': [], 'outputs': []}
    manifest_path = out / 'manifest.json'
    atomic_json(manifest_path, manifest)
    started = time.monotonic()
    digest = hashlib.sha256()
    def check_cancel():
        if cancel.is_set():
            raise ExportCancelled('Export cancelled.')

    chunks = RoiChunkWriter(out, cages, check_cancel)
    manifest.update(compression='NPZ DEFLATE', compression_level=COMPRESSION_LEVEL,
                    archive_workers=min(MAX_ARCHIVE_WORKERS, len(cages)))

    try:
        video_sources_before = None
        if include_video:
            from .video import snapshot_video_sources
            video_sources_before = snapshot_video_sources(root, max_frames, check_cancel)
        atomic_json(out / 'roi_config.json', config)
        with ExitStack() as stack:
            stack.enter_context(chunks)
            writers = []
            for cage in cages:
                folder = out / cage['mouse_id']
                (folder / 'pixels').mkdir(parents=True)
                writer = csv.writer(stack.enter_context((folder / 'temperature_timeseries.csv').open('w', newline='', encoding='utf-8')))
                writer.writerow(['source_frame_index', 'take_id', 'timestamp_utc_ns', 'host_monotonic_ns',
                                 'timestamp_utc_iso', 'mouse_id', 'genotype', 'temperature_c', 'roi_max_c',
                                 'roi_mean_c', 'warmest_5pct_mean_c', 'valid_pixel_count', 'quality', 'calibration_profile_index'])
                writers.append(writer)
                manifest['outputs'].append(str((folder / 'temperature_timeseries.csv').relative_to(out)))
            frames = iter_frames(root, cancel, digest)
            stack.callback(frames.close)
            for frame in frames:
                if list(frame.pixels.shape) != list(shape):
                    raise ValueError('Selection dimensions differ from the recording.')
                if manifest['frames'] == 0:
                    save_overview(out / 'roi_overview.png', frame.pixels, cages)
                profile = {k: frame.metadata.get(k) for k in ('calibration', 'calibration_scale', 'calibration_offset_c', 'temperature_units')}
                if profile not in manifest['calibration_profiles']:
                    manifest['calibration_profiles'].append(profile)
                calibration_id = manifest['calibration_profiles'].index(profile)
                info = [frame.index, frame.take_id, frame.utc_ns, frame.host_ns]
                seconds, ns = divmod(frame.utc_ns, 1_000_000_000)
                iso = datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + f'.{ns:09d}Z'
                crops = chunks.add(frame.pixels, info, calibration_id)
                for cage, crop, writer in zip(cages, crops, writers):
                    stats = temperatures(crop)
                    writer.writerow(info + [iso, cage['mouse_id'], cage['genotype'], stats[0]] + stats + [calibration_id])
                manifest['frames'] += 1
                if chunks.full:
                    chunks.flush()
                if progress:
                    elapsed = time.monotonic() - started
                    fraction = frame.bytes_read / identity['files']['seek_thermal.csv']['size']
                    progress(manifest['frames'], fraction * (0.5 if include_video else 1), elapsed * (1 / fraction - 1))
                if max_frames is not None and manifest['frames'] >= max_frames:
                    break
            if cancel.is_set():
                raise ExportCancelled('Export cancelled.')
            chunks.flush()
        if source_identity(root) != identity:
            raise ValueError('Source files changed during export; results are incomplete.')
        if cancel.is_set():
            raise ExportCancelled('Export cancelled.')
        if include_video:
            from .video import export_video_parts
            def video_progress(done, total):
                if progress:
                    fraction = 0.5 + 0.5 * done / total
                    elapsed = time.monotonic() - started
                    progress(done, fraction, elapsed * (1 / fraction - 1))
            video_outputs, video_sources, video_parts = export_video_parts(
                root, out, cages, shape, manifest['frames'], check_cancel, video_sources_before,
                video_progress, sample=max_frames is not None)
            manifest.update(video_sources=video_sources, video_parts=video_parts,
                            video_note='Color crops preserve native ROI; odd dimensions are padded on bottom/right.')
            manifest['outputs'] += video_outputs
        if source_identity(root) != identity:
            raise ValueError('Source files changed during export; results are incomplete.')
        check_cancel()
        manifest.update(status='complete', elapsed_seconds=time.monotonic() - started,
                        processed_thermal_bytes_sha256=digest.hexdigest(), chunk_frames=chunks.chunk_size)
        manifest['outputs'] += chunks.outputs
        manifest['outputs'] += ['roi_config.json', 'roi_overview.png']
        atomic_json(manifest_path, manifest)
    except Exception as exc:
        manifest['outputs'].extend(path for path in chunks.outputs if path not in manifest['outputs'])
        manifest.update(status='cancelled' if isinstance(exc, ExportCancelled) else 'incomplete', error=str(exc))
        try:
            atomic_json(manifest_path, manifest)
        except OSError:
            pass  # The initial incomplete manifest remains if disk writes fail.
        raise
    return out
