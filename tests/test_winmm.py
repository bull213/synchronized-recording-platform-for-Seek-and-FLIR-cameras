"""Direct MME contract tests with a simulated Win32 driver, not microphone tests."""
import ctypes as C
import json
import sys
import threading
import time
import wave
from types import SimpleNamespace
from pathlib import Path

import pytest

from ultramic_studio import audio, winmm
from ultramic_studio.core import RecorderEngine, RecordingConfig


DEVICE = {"index": 4, "name": "Ultramic 200K 16 bi", "host_api": "MME",
          "backend": "winmm_direct", "mme_device_id": 2}


class FakeWinMM:
    def __init__(self, reject=False, exhaust=False, bad_size=None, fail_queue=False, batches=1):
        self.reject, self.exhaust, self.bad_size = reject, exhaust, bad_size
        self.fail_queue = fail_queue
        self.batches = batches
        self.opens, self.headers, self.queued, self.delivered = [], [], [], []
        self.running = False
        self.waits = 0
        self.event = threading.Event()
        self.closed = self.event_closed = False

    def caps(self, device_id):
        assert device_id == 2
        return SimpleNamespace(name=DEVICE["name"])

    def check(self, code, operation):
        if code:
            raise RuntimeError(f"{operation}: simulated MME {code}")

    def CreateEventW(self, *args):
        return 101

    def waveInOpen(self, handle, device_id, fmt, callback, instance, flags):
        f = fmt._obj
        self.opens.append((device_id, f.nSamplesPerSec, f.nChannels, f.wBitsPerSample,
                           f.nAvgBytesPerSec, f.nBlockAlign, flags))
        if self.reject:
            return 32  # WAVERR_BADFORMAT
        if handle is not None:
            handle._obj.value = 202
        return 0

    def waveInPrepareHeader(self, handle, header, size):
        h = header._obj
        self.headers.append(h)
        h.dwFlags |= winmm.WHDR_PREPARED
        return 0

    def waveInAddBuffer(self, handle, header, size):
        if self.fail_queue:
            return 1
        h = header._obj
        h.dwFlags = winmm.WHDR_PREPARED | winmm.WHDR_INQUEUE
        self.queued.append(h)
        return 0

    def waveInStart(self, handle):
        self.running = True
        return 0

    def complete(self, frames=winmm.BLOCK):
        h = self.queued.pop(0)
        pcm = (len(self.delivered) + 1).to_bytes(2, "little") * frames
        C.memmove(h.lpData, pcm, len(pcm))
        h.dwBytesRecorded = len(pcm) if self.bad_size is None else self.bad_size
        h.dwFlags = winmm.WHDR_PREPARED | winmm.WHDR_DONE
        self.delivered.append(pcm)

    def WaitForSingleObject(self, event, milliseconds):
        if self.waits < self.batches:
            count = winmm.BUFFER_COUNT if self.exhaust else 3
            for _ in range(count):
                self.complete()
            self.waits += 1
            return 0
        self.event.wait(.005)
        return 0x102

    def waveInReset(self, handle):
        if self.running and self.queued and not self.exhaust and self.bad_size is None:
            self.complete(frames=17)
        self.running = False
        for h in self.queued:
            h.dwFlags = winmm.WHDR_PREPARED | winmm.WHDR_DONE
        self.queued.clear()
        return 0

    def waveInUnprepareHeader(self, handle, header, size):
        header._obj.dwFlags &= ~winmm.WHDR_PREPARED
        return 0

    def waveInClose(self, handle):
        self.closed = True
        return 0

    def CloseHandle(self, event):
        self.event_closed = True
        return 1


def wait_until(check):
    limit = time.monotonic() + 3
    while not check() and time.monotonic() < limit:
        time.sleep(.005)
    assert check()


def test_mme_buffers_reach_wav_exactly_and_flush_partial_stop(tmp_path):
    api = FakeWinMM(batches=25)  # wrap and reuse the 32-buffer ring more than twice
    engine = RecorderEngine(RecordingConfig(output=tmp_path, auto_mp3=False, pre_seconds=1,
                                             device=dict(DEVICE)))
    source = winmm.MMESource(engine, DEVICE, api=api)
    try:
        source.start()
        wait_until(lambda: engine.snapshot()["samples"] == 75 * winmm.BLOCK)
        start = engine.request("START")
        source.stop()  # reset returns a 17-frame partial buffer to the writer
        engine.close()
        folder = Path(start["session_path"])
        with wave.open(str(folder / "audio_0001.wav"), "rb") as f:
            pcm = f.readframes(f.getnframes())
            assert f.getframerate() == 200_000
        meta = json.loads((folder / "session.json").read_text())
        assert pcm == b"".join(api.delivered)
        assert meta["status"] == "complete"
        assert source.cursor == 75 * winmm.BLOCK + 17
        assert api.closed and api.event_closed
        assert all(not h.dwFlags & winmm.WHDR_PREPARED for h in api.headers)
        assert api.opens == [
            (2, 200000, 1, 16, 400000, 2, winmm.WAVE_FORMAT_QUERY | winmm.WAVE_FORMAT_DIRECT),
            (2, 200000, 1, 16, 400000, 2, winmm.CALLBACK_EVENT | winmm.WAVE_FORMAT_DIRECT)]
        assert engine.config.device["driver_timestamps_available"] is False
        rows = (folder / "audio_blocks.csv").read_text().splitlines()[1:]
        assert rows and all(row.split(",")[3:5] == ["", ""] for row in rows)
    finally:
        source.stop()
        engine.close()


def test_mme_rejects_format_without_fallback(tmp_path):
    api = FakeWinMM(reject=True)
    engine = RecorderEngine(RecordingConfig(output=tmp_path, auto_mp3=False))
    source = winmm.MMESource(engine, DEVICE, api=api)
    try:
        with pytest.raises(RuntimeError, match="200000"):
            source.start()
        assert len(api.opens) == 1
        assert not api.headers
    finally:
        source.stop()
        engine.close()


@pytest.mark.parametrize("kwargs,fragment", [({"exhaust": True}, "All MME buffers"),
                                             ({"bad_size": 3}, "invalid PCM"),
                                             ({"bad_size": 0}, "short buffer")])
def test_mme_driver_failures_mark_recording_incomplete(tmp_path, kwargs, fragment):
    api = FakeWinMM(**kwargs)
    engine = RecorderEngine(RecordingConfig(output=tmp_path, auto_mp3=False, pre_seconds=0))
    start = engine.request("START")
    source = winmm.MMESource(engine, DEVICE, api=api)
    try:
        source.start()
        wait_until(lambda: not engine.thread.is_alive())
        source.stop()
        meta = json.loads((Path(start["session_path"]) / "session.json").read_text())
        assert meta["status"] == "error"
        assert fragment in engine.snapshot()["error"]
        assert api.closed and api.event_closed
    finally:
        source.stop()
        engine.close()


def test_mme_start_failure_releases_prepared_memory(tmp_path):
    api = FakeWinMM(fail_queue=True)
    engine = RecorderEngine(RecordingConfig(output=tmp_path, auto_mp3=False))
    try:
        with pytest.raises(RuntimeError, match="Queue MME"):
            winmm.MMESource(engine, DEVICE, api=api).start()
        assert api.closed and api.event_closed
        assert all(not h.dwFlags & winmm.WHDR_PREPARED for h in api.headers)
    finally:
        engine.close()


def test_windows_device_enumeration_uses_actual_winmm_ids(monkeypatch):
    monkeypatch.setattr(audio.platform, "system", lambda: "Windows")
    pa = SimpleNamespace(query_hostapis=lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}],
                         query_devices=lambda: [
                             {"name": "Mapper", "hostapi": 0, "max_input_channels": 2},
                             {"name": "Ultramic 200K 16 bi", "hostapi": 0, "max_input_channels": 1},
                             {"name": "Other mic", "hostapi": 1, "max_input_channels": 1,
                              "default_samplerate": 48000}])
    monkeypatch.setitem(sys.modules, "sounddevice", pa)
    monkeypatch.setattr(winmm.WinMM, "__init__", lambda self: None)
    monkeypatch.setattr(winmm.WinMM, "devices", lambda self: [dict(DEVICE)])
    devices = audio.list_devices()
    assert len(devices) == 2
    selected = next(d for d in devices if d["host_api"] == "MME")
    assert selected["mme_device_id"] == 2
    assert selected["index"] == 3  # intentionally differs from WinMM and PortAudio IDs
    assert isinstance(audio.create_input_source(None, selected), winmm.MMESource)


def test_windows_struct_sizes():
    assert C.sizeof(winmm.WaveFormat) == 18
    assert C.sizeof(winmm.WaveInCaps) == 80
    assert C.sizeof(winmm.WaveHeader) == (48 if C.sizeof(C.c_void_p) == 8 else 32)
