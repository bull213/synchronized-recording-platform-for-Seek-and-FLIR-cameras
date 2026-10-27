"""Display-only spectral processing. Recording never passes through this module."""
from __future__ import annotations

import numpy as np


class StreamingSTFT:
    def __init__(self, rate: int = 200_000, nfft: int = 512):
        self.rate, self.nfft = rate, nfft
        self.hop = nfft // 4
        self.window = np.hanning(nfft).astype(np.float32)
        self.scale = 2 / self.window.sum()
        self.buffer = np.empty(0, dtype=np.float32)
        self.buffer_start = 0
        self.next_sample = None

    def feed(self, pcm: bytes, start: int) -> tuple[np.ndarray, np.ndarray]:
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        if self.next_sample != start:
            self.buffer = np.empty(0, dtype=np.float32)
            self.buffer_start = start
        self.next_sample = start + x.size
        self.buffer = np.concatenate((self.buffer, x))
        if self.buffer.size < self.nfft:
            return np.empty(0), np.empty((0, self.nfft // 2 + 1), dtype=np.float32)
        frames = np.lib.stride_tricks.sliding_window_view(self.buffer, self.nfft)[::self.hop]
        spectrum = np.abs(np.fft.rfft(frames * self.window, axis=1)) * self.scale
        spectrum[:, 0] *= .5
        spectrum[:, -1] *= .5
        db = 20 * np.log10(np.maximum(spectrum, 1e-7))
        times = (self.buffer_start + np.arange(len(frames)) * self.hop + self.nfft / 2) / self.rate
        used = len(frames) * self.hop
        self.buffer = self.buffer[used:].copy()
        self.buffer_start += used
        return times, db.astype(np.float32)


def file_spectrogram(x: np.ndarray, rate: int, nfft: int = 512, max_columns: int = 2400):
    """Bounded display; max-pool adjacent FFT frames to retain short calls."""
    if x.size < nfft:
        x = np.pad(x, (0, nfft - x.size))
    hop = nfft // 4
    frames = np.lib.stride_tricks.sliding_window_view(x, nfft)[::hop]
    grouping = max(1, int(np.ceil(len(frames) / max_columns)))
    window = np.hanning(nfft)
    chunks = []
    # Chunk transform work to cap temporary RAM on 60-second views.
    batch = max(1, 256 // grouping) * grouping
    for a in range(0, len(frames), batch):
        spec = np.abs(np.fft.rfft(frames[a:a + batch] * window, axis=1)) * (2 / window.sum())
        spec[:, 0] *= .5
        spec[:, -1] *= .5
        pad = (-len(spec)) % grouping
        if pad:
            spec = np.pad(spec, ((0, pad), (0, 0)), constant_values=0)
        chunks.append(spec.reshape(-1, grouping, nfft // 2 + 1).max(axis=1))
    db = (20 * np.log10(np.maximum(np.concatenate(chunks), 1e-7))).astype(np.float32)
    return db, grouping * hop / rate
