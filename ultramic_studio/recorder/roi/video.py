"""Export per-cage Seek video parts using the recorder's frame index."""
from __future__ import annotations

import csv
import math
from contextlib import ExitStack
from pathlib import Path

import cv2


def _source(root, name):
    path = (root / name.replace('\\', '/')).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f'Seek video part is missing or outside the recording: {name}')
    return path


def _identity(path):
    stat = path.stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def snapshot_video_sources(root, max_frames=None, check_cancel=None):
    """Capture each referenced source's identity before extraction starts."""
    root = Path(root).resolve()
    identities = {}
    with (root / 'seek_log.csv').open(newline='', encoding='utf-8-sig') as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            if check_cancel:
                check_cancel()
            if max_frames is not None and index >= max_frames:
                break
            name = row['file'].replace('\\', '/')
            if name not in identities:
                identities[name] = _identity(_source(root, name))
    return identities


def export_video_parts(root, out, cages, shape, count, check_cancel, source_identities, progress=None, *, sample=False):
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
            raise ValueError(f'Seek video has frames missing from its log: {current}')
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

    try:
        with ExitStack() as stack:
            maps = []
            for cage in cages:
                folder = out / cage['mouse_id']
                (folder / 'video_parts').mkdir(exist_ok=True)
                path = folder / 'video_frame_map.csv'
                writer = csv.writer(stack.enter_context(path.open('w', newline='', encoding='utf-8')))
                writer.writerow(['source_frame_index', 'take_id', 'timestamp_utc_ns', 'host_monotonic_ns',
                                 'source_video', 'source_part_frame_index', 'output_video', 'output_part_frame_index'])
                maps.append(writer)
                outputs.append(str(path.relative_to(out)))
            with (root / 'seek_log.csv').open(newline='', encoding='utf-8-sig') as handle:
                for number, row in enumerate(csv.DictReader(handle)):
                    if number >= count:
                        break
                    check_cancel()
                    name = row['file'].replace('\\', '/')
                    index = int(row['part_frame_index'])
                    if name != current:
                        finish_part()
                        if name in completed:
                            raise ValueError(f'Seek video part is not contiguous: {name}')
                        completed.add(name)
                        source = _source(root, name)
                        identities[name] = _identity(source)
                        if identities[name] != source_identities.get(name):
                            raise ValueError(f'Seek video changed during export: {name}')
                        capture = cv2.VideoCapture(str(source))
                        fps = capture.get(cv2.CAP_PROP_FPS)
                        if not capture.isOpened() or not math.isfinite(fps) or fps <= 0:
                            raise ValueError(f'Cannot open Seek video or determine frame rate: {name}')
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
                    if index != part_frames:
                        raise ValueError(f'Unexpected Seek frame index {index} in {name}; expected {part_frames}.')
                    ok, image = capture.read()
                    if not ok or image.shape[0] not in (input_shape[0], input_shape[0] + input_shape[0] % 2) or image.shape[1] not in (input_shape[1], input_shape[1] + input_shape[1] % 2):
                        raise ValueError(f'Seek video frame missing or has incorrect dimensions: {name} frame {index}.')
                    for cage, writer, mapping, (_, final, _) in zip(cages, writers, maps, part_paths):
                        x, y, width, height = cage['roi']
                        crop = image[y:y + height, x:x + width]
                        if height % 2 or width % 2:
                            crop = cv2.copyMakeBorder(crop, 0, height % 2, 0, width % 2, cv2.BORDER_REPLICATE)
                        writer.write(crop)
                        mapping.writerow([row['idx'], row['take_id'], row['sdk_utc_ns'],
                                          row['host_received_monotonic_ns'], name, index,
                                          str(final.relative_to(out / cage['mouse_id'])), part_frames])
                    part_frames += 1
                    if progress:
                        progress(number + 1, count)
            finish_part(last=True)
            if sum(part['frames'] for part in parts) != count:
                raise ValueError('Seek video frame count does not match thermal data.')
        for name, identity in identities.items():
            if _identity(_source(root, name)) != identity:
                raise ValueError(f'Seek video changed during export: {name}')
        return outputs, identities, parts
    finally:
        if capture is not None:
            capture.release()
        for writer in writers:
            writer.release()
