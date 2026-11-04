"""Bounded, verified NPZ chunk storage, independent of ROI selection and parsing."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import numpy as np


COMPRESSION_LEVEL = 1
MAX_ARCHIVE_WORKERS = 2
PIXEL_BUFFER_BYTES = 64 * 1024 ** 2
MAX_CHUNK_FRAMES = 256


def write_verified_archive(target, arrays, check_cancel):
    """Publish a standard NumPy archive only after checking every stored array."""
    partial = target.with_suffix('.partial')
    check_cancel()
    with ZipFile(partial, 'w', compression=ZIP_DEFLATED, compresslevel=COMPRESSION_LEVEL,
                 allowZip64=True) as archive:
        for name, array in arrays.items():
            check_cancel()
            with archive.open(name + '.npy', 'w', force_zip64=True) as member:
                np.lib.format.write_array(member, np.ascontiguousarray(array), allow_pickle=False)
    with np.load(partial, allow_pickle=False) as saved:
        if set(saved.files) != set(arrays):
            raise OSError('Pixel archive members do not match the export.')
        for name, expected in arrays.items():
            check_cancel()
            actual = saved[name]
            if (actual.dtype != expected.dtype or actual.shape != expected.shape
                    or not np.array_equal(actual, expected, equal_nan=True)):
                raise OSError(f'Pixel archive verification failed: {name}')
    check_cancel()
    partial.replace(target)
    return target


class RoiChunkWriter:
    """Reuse pixel buffers; at most two cage archives compress concurrently.

    flush() waits before buffers are reused. No growing queue of frame chunks is
    retained, and worker exceptions propagate before an export can be complete.
    """
    def __init__(self, output, cages, check_cancel):
        self.output = Path(output)
        self.cages = cages
        self.check_cancel = check_cancel
        bytes_per_frame = 8 * sum(c['roi'][2] * c['roi'][3] for c in cages)
        self.chunk_size = max(1, min(MAX_CHUNK_FRAMES, PIXEL_BUFFER_BYTES // bytes_per_frame))
        self.buffers = [np.empty((self.chunk_size, c['roi'][3], c['roi'][2]), dtype=np.float64) for c in cages]
        self.info = np.empty((self.chunk_size, 4), dtype=np.int64)
        self.calibration_ids = np.empty(self.chunk_size, dtype=np.int32)
        self.count = self.part = 0
        self.outputs = []
        self.executor = None

    def __enter__(self):
        self.executor = ThreadPoolExecutor(max_workers=min(MAX_ARCHIVE_WORKERS, len(self.cages)),
                                           thread_name_prefix='ROI archive')
        return self

    def __exit__(self, *args):
        self.executor.shutdown(wait=True, cancel_futures=True)

    def add(self, pixels, info, calibration_id):
        """Return contiguous crop views, valid until the next flush/reuse."""
        self.check_cancel()
        if self.count >= self.chunk_size:
            raise RuntimeError('Flush a full chunk before adding more frames.')
        crops = []
        for cage, buffer in zip(self.cages, self.buffers):
            x, y, w, h = cage['roi']
            crop = buffer[self.count]
            np.copyto(crop, pixels[y:y+h, x:x+w])
            crops.append(crop)
        self.info[self.count] = info
        self.calibration_ids[self.count] = calibration_id
        self.count += 1
        return crops

    @property
    def full(self):
        return self.count == self.chunk_size

    def flush(self):
        if not self.count:
            return
        self.check_cancel()
        self.part += 1
        info = self.info[:self.count]
        shared = dict(source_frame_index=info[:, 0], take_id=info[:, 1],
                      timestamp_utc_ns=info[:, 2], host_monotonic_ns=info[:, 3],
                      calibration_profile_index=self.calibration_ids[:self.count])
        jobs = []
        for cage, buffer in zip(self.cages, self.buffers):
            target = self.output / cage['mouse_id'] / 'pixels' / f'part_{self.part:06d}.npz'
            arrays = dict(pixels=buffer[:self.count], **shared)
            jobs.append(self.executor.submit(write_verified_archive, target, arrays, self.check_cancel))
        # Record already-published files even if another worker fails.
        failure = None
        for job in jobs:
            try:
                target = job.result()
                self.outputs.append(str(target.relative_to(self.output)))
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure
        self.count = 0
