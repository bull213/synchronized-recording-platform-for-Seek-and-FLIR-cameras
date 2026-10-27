"""Bounded-memory normal-speed or 10x time-expanded MP3 export."""
from __future__ import annotations

import json
import os
import uuid
import wave
from pathlib import Path

from .core import atomic_json, utc_now


def export_mp3(wav_paths: list[Path], destination: Path, progress=None, *,
               time_expansion: int = 10, write_metadata: bool = True) -> Path:
    import lameenc
    if time_expansion not in (1, 10):
        raise ValueError('Time expansion must be 1 (normal speed) or 10.')
    if not wav_paths:
        raise ValueError("No WAV audio exists in this recording.")
    if destination.exists():
        raise FileExistsError(f"File already exists: {destination.name}")
    rate = None
    total = 0
    for path in wav_paths:
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getcomptype() != "NONE":
                raise ValueError("MP3 export supports uncompressed mono 16-bit WAV files.")
            if rate is None:
                rate = w.getframerate()
            if rate != w.getframerate() or rate != 200_000:
                raise ValueError("This listening export requires 200,000 Hz WAV files.")
            total += w.getnframes()
    if not total:
        raise ValueError("The selected recording contains no audio samples.")
    encoder = lameenc.Encoder()
    encoder.set_in_sample_rate(rate // time_expansion)
    encoder.set_out_sample_rate(44_100)  # LAME performs final audible resampling.
    encoder.set_channels(1)
    encoder.set_bit_rate(128)
    encoder.set_quality(2)
    encoder.silence()
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex[:10] + ".partial")
    count = 0
    reserved = False
    try:
        with temporary.open("xb") as out:
            for path in wav_paths:
                with wave.open(str(path), "rb") as w:
                    while pcm := w.readframes(200_000):
                        out.write(encoder.encode(pcm))
                        count += len(pcm) // 2
                        if progress:
                            progress(count / total)
            out.write(encoder.flush())
            out.flush()
            os.fsync(out.fileno())
        # Exclusive reservation avoids overwriting a file created during export.
        with destination.open("xb"):
            pass
        reserved = True
        os.replace(temporary, destination)
        reserved = False
    except Exception:
        temporary.unlink(missing_ok=True)
        if reserved:
            destination.unlink(missing_ok=True)
        raise
    if write_metadata:
        atomic_json(destination.with_suffix(".mp3.json"), {
            "created_utc": utc_now(), "source_files": [p.name for p in wav_paths],
            "source_rate_hz": rate, "source_samples": total, "time_expansion_factor": time_expansion,
            "mp3_rate_hz": 44_100, "bitrate_kbps": 128,
            "note": f"Listening copy only. Time expansion: {time_expansion}, plus MP3 encoder padding. "
                    "Use the original WAV files for analysis and event alignment.",
        })
    return destination


def export_session(session: Path, progress=None) -> Path:
    meta = json.loads((session / "session.json").read_text(encoding="utf-8"))
    if meta["status"] == "recording":
        raise RuntimeError("Stop recording before exporting the session.")
    wavs = [session / x["file"] for x in meta["segments"]]
    if any(p.parent.resolve() != session.resolve() for p in wavs):
        raise ValueError("Invalid path in session manifest.")
    target = session / "listening_10x.mp3"
    if target.exists():
        if target.stat().st_size == 0 or not target.with_suffix(".mp3.json").exists():
            raise RuntimeError("An incomplete MP3 export already exists. Remove or rename it before retrying.")
        return target
    return export_mp3(wavs, target, progress)
