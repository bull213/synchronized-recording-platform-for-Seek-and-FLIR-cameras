"""Single-owner recording worker; no GUI or audio-driver dependencies.

Audio and commands share one FIFO. A trigger boundary is the next queued audio
block, NOT a hardware-synchronized TTL sample. Original PCM is never resampled.
"""
from __future__ import annotations

import csv
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
import wave
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def safe_label(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())[:64].strip("._") or "session"


def atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


@dataclass(frozen=True)
class AudioBlock:
    pcm: bytes
    start: int
    adc_time: float | None = None
    callback_time: float | None = None
    received_ns: int = field(default_factory=time.perf_counter_ns)

    @property
    def frames(self) -> int:
        return len(self.pcm) // 2

    def tail(self, frames: int, rate: int) -> AudioBlock:
        skipped = self.frames - frames
        return AudioBlock(self.pcm[skipped * 2:], self.start + skipped,
                          None if self.adc_time is None else self.adc_time + skipped / rate,
                          self.callback_time, self.received_ns)


@dataclass
class RecordingConfig:
    output: Path
    label: str = "mouse"
    serial: str = ""
    rate: int = 200_000
    pre_seconds: float = 0.5
    segment_seconds: int = 1800
    auto_mp3: bool = True
    device: dict[str, Any] = field(default_factory=dict)
    demo: bool = False

    def validate(self) -> None:
        if self.rate != 200_000:
            raise ValueError("This build is configured for the Ultramic 200K: 200,000 samples/s.")
        if not 0 <= self.pre_seconds <= 5:
            raise ValueError("Pre-trigger must be between 0 and 5 seconds.")
        if not 1 <= self.segment_seconds <= 1800:
            raise ValueError("WAV segments must be between 1 and 1800 seconds.")


class SessionWriter:
    """Owns disk files. Only the capture worker may call this object."""

    def __init__(self, config: RecordingConfig, boundary: int, pre: list[AudioBlock],
                 trigger: dict, label: str | None = None, destination: Path | None = None):
        self.config = config
        config.output.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(config.output).free < 100_000_000:
            raise OSError("Less than 100 MB free in the recording folder.")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = ("DEMO_" if config.demo else "") + safe_label(label or config.label)
        self.path = destination or config.output / f"{stamp}_{name}_{uuid.uuid4().hex[:6]}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.mkdir(exist_ok=False)
        self.total = 0
        self.wave: wave.Wave_write | None = None
        self.raw = None
        self.segment_frames = 0
        self.segment_limit = config.rate * config.segment_seconds
        self.segments: list[dict] = []
        self.last_sync = time.monotonic()
        self.closed = False
        self.events = None
        self.blocks = None
        self.meta = {
            "software": "Ultramic Studio", "software_version": __version__,
            "status": "recording", "created_utc": utc_now(), "demo": config.demo,
            "microphone_model": "Dodotronic Ultramic 200K",
            "microphone_serial_user_entered": config.serial,
            "serial_verified_by_software": False, "audio_device": config.device,
            "sample_rate_hz": config.rate, "channels": 1, "bits_per_sample": 16,
            "encoding": "PCM signed 16-bit little endian", "processing": "none",
            "label": label or config.label, "pretrigger_requested_seconds": config.pre_seconds,
            "pretrigger_samples": sum(b.frames for b in pre),
            "first_stream_sample": pre[0].start if pre else boundary,
            "start_boundary_stream_sample": boundary,
            "timing_note": "Commands use host-arrival FIFO block boundaries. TTL is asynchronous to the USB audio clock; no sample-accurate synchronization is claimed. ADC timestamps are host-driver reports, not TTL timestamps.",
            "segments": self.segments, "recorded_samples": 0, "errors": [],
            "auto_mp3_requested": config.auto_mp3,
            "mp3_note": "Optional 10x time-expanded listening copy. WAV is the unmodified analysis source.",
        }
        try:
            self.events = (self.path / "events.jsonl").open("w", encoding="utf-8", buffering=1)
            self.blocks = (self.path / "audio_blocks.csv").open("w", encoding="utf-8", newline="")
            self.block_csv = csv.writer(self.blocks)
            self.block_csv.writerow(["stream_sample", "file_sample", "frames", "adc_time_seconds",
                                     "callback_time_seconds", "host_received_monotonic_ns"])
            atomic_json(self.path / "session.json", self.meta)
            for block in pre:
                self.write(block)
            self.event("START", trigger, boundary)
        except Exception:
            self.close("error", "Could not initialize the recording files.")
            raise

    def _open_segment(self) -> None:
        index = len(self.segments) + 1
        name = f"audio_{index:04d}.wav"
        self.raw = (self.path / name).open("xb")
        self.wave = wave.open(self.raw, "wb")
        self.wave.setparams((1, 2, self.config.rate, 0, "NONE", "not compressed"))
        self.segment_frames = 0
        self.segments.append({"file": name, "first_file_sample": self.total, "samples": 0})

    def _close_segment(self) -> None:
        try:
            if self.wave is not None:
                self.wave.close()
        finally:
            self.wave = None
            if self.raw is not None:
                try:
                    self.raw.flush()
                    os.fsync(self.raw.fileno())
                finally:
                    self.raw.close()
                    self.raw = None

    def event(self, command: str, info: dict, boundary: int) -> None:
        item = {**info, "command": command, "boundary_stream_sample": boundary,
                "file_sample": self.total, "written_utc": utc_now()}
        if self.events:
            self.events.write(json.dumps(item, ensure_ascii=False) + "\n")

    def write(self, block: AudioBlock) -> None:
        self.block_csv.writerow([block.start, self.total, block.frames, block.adc_time,
                                 block.callback_time, block.received_ns])
        offset = 0
        while offset < block.frames:
            if self.wave is None:
                self._open_segment()
            n = min(block.frames - offset, self.segment_limit - self.segment_frames)
            # writeframes (not writeframesraw) updates the WAV header every block.
            self.wave.writeframes(block.pcm[2 * offset:2 * (offset + n)])
            self.total += n
            self.segment_frames += n
            self.segments[-1]["samples"] = self.segment_frames
            offset += n
            if self.segment_frames == self.segment_limit:
                self._close_segment()
        if time.monotonic() - self.last_sync >= 1:
            if self.raw:
                self.raw.flush()
            self.blocks.flush()
            self.meta["recorded_samples"] = self.total
            atomic_json(self.path / "session.json", self.meta)
            self.last_sync = time.monotonic()
            if shutil.disk_usage(self.path).free < 100_000_000:
                raise OSError("Recording stopped: less than 100 MB free disk space.")

    def close(self, status: str = "complete", error: str | None = None) -> Path:
        if self.closed:
            return self.path
        self.closed = True
        failures = []
        try:
            self._close_segment()
        except Exception as exc:
            failures.append(str(exc))
        for handle in (self.events, self.blocks):
            if handle:
                try:
                    handle.close()
                except Exception as exc:
                    failures.append(str(exc))
        self.meta.update(status="error" if error or failures else status,
                         stopped_utc=utc_now(), recorded_samples=self.total,
                         duration_seconds=self.total / self.config.rate)
        self.meta["errors"].extend(([error] if error else []) + failures)
        atomic_json(self.path / "session.json", self.meta)
        return self.path


class RecorderEngine:
    def __init__(self, config: RecordingConfig, queue_blocks: int = 512):
        config.validate()
        self.config = config
        self.inbox: queue.Queue = queue.Queue(queue_blocks)
        self.preview: queue.Queue[AudioBlock] = queue.Queue(128)
        self.notices: queue.SimpleQueue[dict] = queue.SimpleQueue()
        self.pre: deque[AudioBlock] = deque()
        self.pre_frames = 0
        self.session: SessionWriter | None = None
        self.cursor = 0
        self._fault: str | None = None
        self._lock = threading.Lock()
        self._state = dict(connected=True, armed=False, recording=False, samples=0,
                           recorded_samples=0, session_path=None, error=None, preview_drops=0)
        self.thread = threading.Thread(target=self._run, name="WAV writer", daemon=True)
        self.thread.start()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _update(self, **kwargs) -> None:
        with self._lock:
            self._state.update(kwargs)

    def fault(self, message: str) -> None:
        if self._fault is None:
            self._fault = message

    def feed(self, block: AudioBlock) -> bool:
        if self._fault or not self.thread.is_alive():
            return False
        try:
            self.inbox.put_nowait(block)
            return True
        except queue.Full:
            self.fault("The recording queue overflowed. Capture stopped; data are incomplete.")
            return False

    def request(self, command: str, source: str = "manual", timeout: float = 5, **kwargs) -> dict:
        if command.upper() == "STATUS":
            return {"ok": True, **self.snapshot()}
        if not self.thread.is_alive() or self._fault:
            return {"ok": False, "error": self._fault or "Microphone is disconnected."}
        future: Future = Future()
        info = {"source": source, "received_utc": utc_now(),
                "received_monotonic_ns": time.perf_counter_ns(), **kwargs}
        try:
            self.inbox.put_nowait((command.upper(), info, future))
        except queue.Full:
            self.fault("The command queue overflowed. Capture stopped.")
            return {"ok": False, "error": "Capture queue is full."}
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            return {"ok": False, "error": "Command timed out. Check STATUS before retrying."}

    def close(self) -> None:
        if self.thread.is_alive():
            self.request("CLOSE")
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError("The recording writer is still closing. Do not exit yet.")

    def _finish(self, info: dict, error: str | None = None) -> Path | None:
        if self.session is None:
            return None
        session, self.session = self.session, None
        try:
            session.event("ERROR" if error else "STOP", info, self.cursor)
        finally:
            path = session.close(error=error)
        self._update(recording=False, recorded_samples=session.total)
        self.notices.put({"type": "saved", "path": str(path), "error": error,
                          "auto_mp3": self.config.auto_mp3 and not error})
        return path

    def _handle(self, command: str, info: dict) -> dict:
        state = self.snapshot()
        external = info["source"] != "manual"
        if external and command not in ("START", "STOP", "MARK"):
            return {"ok": False, "error": "External commands: START, STOP, MARK, STATUS only."}
        if external and not state["armed"]:
            return {"ok": False, "error": "Triggers are disarmed. Arm them in the app first."}
        if command == "ARM":
            self._update(armed=True)
        elif command == "DISARM":
            self._finish(info)
            self._update(armed=False)
        elif command == "START":
            if self.session is not None:
                self.session.event("START_IGNORED_ALREADY_RECORDING", info, self.cursor)
                return {"ok": True, "already_recording": True, **self.snapshot()}
            self.session = SessionWriter(self.config, self.cursor, list(self.pre), info,
                                         info.get("label"))
            self._update(recording=True, session_path=str(self.session.path),
                         recorded_samples=self.session.total)
            self.notices.put({"type": "started", "path": str(self.session.path)})
        elif command == "STOP":
            self._finish(info)
        elif command == "MARK":
            if self.session is None:
                return {"ok": False, "error": "No recording is active."}
            self.session.event("MARK", info, self.cursor)
        elif command == "CLOSE":
            self._finish(info)
            self._update(armed=False, connected=False)
        else:
            return {"ok": False, "error": f"Unknown command: {command}"}
        return {"ok": True, **self.snapshot()}

    def _audio(self, block: AudioBlock) -> None:
        if len(block.pcm) % 2:
            raise ValueError("Incomplete PCM sample.")
        if block.start != self.cursor:
            raise RuntimeError(f"Audio discontinuity: expected sample {self.cursor}, received {block.start}.")
        if self.session:
            self.session.write(block)
            self._update(recorded_samples=self.session.total)
        self.cursor += block.frames
        self._update(samples=self.cursor)
        keep = round(self.config.pre_seconds * self.config.rate)
        if keep:
            self.pre.append(block)
            self.pre_frames += block.frames
            while self.pre and self.pre_frames - self.pre[0].frames >= keep:
                self.pre_frames -= self.pre.popleft().frames
            if self.pre_frames > keep:
                first = self.pre.popleft()
                self.pre.appendleft(first.tail(first.frames - (self.pre_frames - keep), self.config.rate))
                self.pre_frames = keep
        try:
            self.preview.put_nowait(block)
        except queue.Full:
            try:
                self.preview.get_nowait()
            except queue.Empty:
                pass
            self.preview.put_nowait(block)
            self._update(preview_drops=self.snapshot()["preview_drops"] + 1)

    def _run(self) -> None:
        try:
            while True:
                if self._fault:
                    raise RuntimeError(self._fault)
                try:
                    item = self.inbox.get(timeout=0.1)
                except queue.Empty:
                    continue
                if isinstance(item, AudioBlock):
                    self._audio(item)
                else:
                    command, info, future = item
                    if not future.set_running_or_notify_cancel():
                        continue
                    try:
                        result = self._handle(command, info)
                    except Exception as exc:
                        future.set_result({"ok": False, "error": str(exc)})
                        raise
                    future.set_result(result)
                    if command == "CLOSE":
                        break
        except Exception as exc:
            self._fault = str(exc)
            try:
                self._finish({"source": "system", "received_utc": utc_now()}, self._fault)
            except Exception as close_exc:
                self._fault += f"; finalizing files: {close_exc}"
            self._update(error=self._fault, connected=False, recording=False, armed=False)
            self.notices.put({"type": "error", "message": self._fault})
        finally:
            self._update(connected=False)
            while True:
                try:
                    item = self.inbox.get_nowait()
                except queue.Empty:
                    break
                if not isinstance(item, AudioBlock):
                    future = item[2]
                    if not future.done():
                        future.set_result({"ok": False, "error": self._fault or "Disconnected."})
