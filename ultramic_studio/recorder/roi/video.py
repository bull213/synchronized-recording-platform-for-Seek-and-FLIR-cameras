"""Export per-cage Seek/FLIR video parts using the recorder's frame index."""
from __future__ import annotations

import csv
import math
from contextlib import ExitStack
from pathlib import Path

import cv2


def _source(root, name, stream_name='Seek'):
    path = (root / name.replace('\\', '/')).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f'{stream_name} video part is missing or outside the recording: {name}')
    return path


def _identity(path):
    stat = path.stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def check_video_sources_exist(root, log_file='seek_log.csv', video_files=None):
    """Return True if at least one video source referenced by the log or video_files exists on disk."""
    root = Path(root).resolve()
    if video_files:
        return any((root / f.replace('\\', '/')).is_file() for f in video_files)
    log_path = root / log_file
    if not log_path.is_file():
        return False
    try:
        with log_path.open(newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(handle)
            row = next(reader, None)
            if row:
                vfile = row.get('file') or row.get('video_file')
                if vfile:
                    path = (root / vfile.replace('\\', '/')).resolve()
                    return path.is_file() and path.is_relative_to(root)
    except Exception:
        pass
    return False


def snapshot_video_sources(root, max_frames=None, check_cancel=None, log_file='seek_log.csv', video_files=None, stream_name='Seek'):
    """Capture each referenced source's identity before extraction starts."""
    root = Path(root).resolve()
    identities = {}

    if video_files and (not log_file or not (root / log_file).is_file()):
        for name in video_files:
            if check_cancel:
                check_cancel()
            norm = name.replace('\\', '/')
            if norm not in identities:
                identities[norm] = _identity(_source(root, norm, stream_name))
        return identities

    log_path = root / log_file
    if not log_path.is_file():
        if video_files:
            for name in video_files:
                if check_cancel:
                    check_cancel()
                norm = name.replace('\\', '/')
                if norm not in identities:
                    identities[norm] = _identity(_source(root, norm, stream_name))
        return identities

    with log_path.open(newline='', encoding='utf-8-sig') as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            if check_cancel:
                check_cancel()
            if max_frames is not None and index >= max_frames:
                break
            vfile = row.get('file') or row.get('video_file')
            if not vfile:
                continue
            name = vfile.replace('\\', '/')
            if name not in identities:
                identities[name] = _identity(_source(root, name, stream_name))
    return identities


def export_video_parts(root, out, cages, shape, count, check_cancel, source_identities, progress=None, *,
                       sample=False, log_file='seek_log.csv', stream_name='Seek', video_files=None):
    """Crop indexed color frames, preserving source part boundaries and frame order."""
    root, out = Path(root).resolve(), Path(out)
    identities, outputs, parts = {}, [], []
    completed = set()
    capture = None
    writers = []
    current = None
    part_frames = 0
    part_paths = []
    input_shape = tuple(shape)

    def finish_part(*, last=False):
        nonlocal capture, writers
        if capture is None:
            return
        if not (sample and last) and capture.read()[0]:
            raise ValueError(f'{stream_name} video has frames missing from its log: {current}')
        capture.release()
        capture = None
        for writer in writers:
            writer.release()
        writers = []
        for partial, final, expected_shape in part_paths:
            check_cancel()
            check = cv2.VideoCapture(str(partial))
            try:
                actual = 0
                while True:
                    check_cancel()
                    ok, frame = check.read()
                    if not ok:
                        break
                    if frame.shape[:2] != expected_shape:
                        raise ValueError(f'Invalid output frame size: {partial}')
                    actual += 1
                if actual != part_frames:
                    raise ValueError(f'Video verification failed: {partial} ({actual}/{part_frames} frames).')
            finally:
                check.release()
            partial.replace(final)
            outputs.append(str(final.relative_to(out)))
        parts.append({'source': current, 'frames': part_frames})

    def open_video_part(name):
        nonlocal current, part_frames, part_paths, capture, writers
        finish_part()
        if name in completed:
            raise ValueError(f'{stream_name} video part is not contiguous: {name}')
        completed.add(name)
        source = _source(root, name, stream_name)
        identities[name] = _identity(source)
        if identities[name] != source_identities.get(name):
            raise ValueError(f'{stream_name} video changed during export: {name}')
        capture = cv2.VideoCapture(str(source))
        fps = capture.get(cv2.CAP_PROP_FPS)
        if not capture.isOpened() or not math.isfinite(fps) or fps <= 0:
            raise ValueError(f'Cannot open {stream_name} video or determine frame rate: {name}')
        current, part_frames, part_paths = name, 0, []
        for cage in cages:
            _, _, width, height = cage['roi']
            encoded = (height + height % 2, width + width % 2)
            final = out / cage['mouse_id'] / 'video_parts' / Path(name).name
            partial = final.with_name(final.stem + '.partial.mp4')
            if final.exists() or partial.exists():
                raise ValueError(f'Conflicting video part name: {name}')
            writer = cv2.VideoWriter(str(partial), cv2.VideoWriter_fourcc(*'mp4v'), fps,
                                     (encoded[1], encoded[0]))
            writers.append(writer)
            if not writer.isOpened():
                raise OSError(f'Cannot initialize video encoder: {partial}')
            part_paths.append((partial, final, encoded))

    try:
        with ExitStack() as stack:
            maps = []
            for cage in cages:
                folder = out / cage['mouse_id']
                (folder / 'video_parts').mkdir(parents=True, exist_ok=True)
                path = folder / 'video_frame_map.csv'
                writer = csv.writer(stack.enter_context(path.open('w', newline='', encoding='utf-8')))
                writer.writerow(['source_frame_index', 'take_id', 'timestamp_utc_ns', 'host_monotonic_ns',
                                 'source_video', 'source_part_frame_index', 'output_video', 'output_part_frame_index'])
                maps.append(writer)
                outputs.append(str(path.relative_to(out)))

            log_path = root / log_file if log_file else None
            has_log = log_path is not None and log_path.is_file()

            if has_log:
                with log_path.open(newline='', encoding='utf-8-sig') as handle:
                    for number, row in enumerate(csv.DictReader(handle)):
                        if count is not None and number >= count:
                            break
                        check_cancel()
                        vfile = row.get('file') or row.get('video_file')
                        if not vfile and video_files:
                            vfile = video_files[0]
                        name = vfile.replace('\\', '/')
                        index = int(row.get('part_frame_index', part_frames))
                        if name != current:
                            open_video_part(name)
                        if index != part_frames:
                            raise ValueError(f'Unexpected {stream_name} frame index {index} in {name}; expected {part_frames}.')
                        ok, image = capture.read()
                        if not ok or image.shape[0] not in (input_shape[0], input_shape[0] + input_shape[0] % 2) or image.shape[1] not in (input_shape[1], input_shape[1] + input_shape[1] % 2):
                            raise ValueError(f'{stream_name} video frame missing or has incorrect dimensions: {name} frame {index}.')
                        for cage, writer, mapping, (_, final, _) in zip(cages, writers, maps, part_paths):
                            x, y, width, height = cage['roi']
                            crop = image[y:y + height, x:x + width]
                            if height % 2 or width % 2:
                                crop = cv2.copyMakeBorder(crop, 0, height % 2, 0, width % 2, cv2.BORDER_REPLICATE)
                            writer.write(crop)
                            utc_ts = row.get('sdk_utc_ns') or row.get('cam_ts_ns') or row.get('pc_time', '')
                            host_mono = row.get('host_received_monotonic_ns', '')
                            take_id = row.get('take_id', '1')
                            idx = row.get('idx', str(number))
                            mapping.writerow([idx, take_id, utc_ts, host_mono, name, index,
                                              str(final.relative_to(out / cage['mouse_id'])), part_frames])
                        part_frames += 1
                        if progress:
                            progress(number + 1, count or (number + 1))
            elif video_files:
                number = 0
                for vfile in video_files:
                    name = vfile.replace('\\', '/')
                    open_video_part(name)
                    while True:
                        if count is not None and number >= count:
                            break
                        check_cancel()
                        ok, image = capture.read()
                        if not ok:
                            break
                        if image.shape[0] not in (input_shape[0], input_shape[0] + input_shape[0] % 2) or image.shape[1] not in (input_shape[1], input_shape[1] + input_shape[1] % 2):
                            raise ValueError(f'{stream_name} video frame missing or has incorrect dimensions: {name} frame {part_frames}.')
                        for cage, writer, mapping, (_, final, _) in zip(cages, writers, maps, part_paths):
                            x, y, width, height = cage['roi']
                            crop = image[y:y + height, x:x + width]
                            if height % 2 or width % 2:
                                crop = cv2.copyMakeBorder(crop, 0, height % 2, 0, width % 2, cv2.BORDER_REPLICATE)
                            writer.write(crop)
                            mapping.writerow([number, 1, '', '', name, part_frames,
                                              str(final.relative_to(out / cage['mouse_id'])), part_frames])
                        part_frames += 1
                        number += 1
                        if progress:
                            progress(number, count or number)

            finish_part(last=True)
            if count is not None and sum(part['frames'] for part in parts) != count:
                raise ValueError(f'{stream_name} video frame count does not match thermal data.')

        for name, identity in identities.items():
            if _identity(_source(root, name, stream_name)) != identity:
                raise ValueError(f'{stream_name} video changed during export: {name}')
        return outputs, identities, parts
    finally:
        if capture is not None:
            capture.release()
        for writer in writers:
            writer.release()
