import json
import socket
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from ultramic_studio.core import AudioBlock, RecorderEngine, RecordingConfig
from ultramic_studio.dsp import StreamingSTFT, file_spectrogram
from ultramic_studio.export import export_session
from ultramic_studio.triggers import SoftwareTrigger, parse_command


def wait_until(check, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(.01)
    assert check(), "Condition timed out"


def config(tmp_path, **kwargs):
    return RecordingConfig(output=tmp_path, auto_mp3=False, **kwargs)


def read_session(path):
    meta = json.loads((path / "session.json").read_text())
    pcm = bytearray()
    for part in meta["segments"]:
        with wave.open(str(path / part["file"]), "rb") as f:
            assert f.getframerate() == 200_000
            assert f.getsampwidth() == 2
            assert f.getnchannels() == 1
            assert f.getnframes() == part["samples"]
            pcm.extend(f.readframes(f.getnframes()))
    return meta, bytes(pcm)


def test_raw_pcm_pretrigger_and_segment_continuity(tmp_path):
    """Byte comparison across segment boundaries, including non-block-aligned history."""
    engine = RecorderEngine(config(tmp_path, pre_seconds=.0073, segment_seconds=1))
    rng = np.random.default_rng(1)
    data = rng.integers(-32768, 32768, 435_021, dtype=np.int16).astype("<i2")
    assert engine.feed(AudioBlock(data[:3123].tobytes(), 0))
    engine.request("ARM")  # FIFO barrier after audio.
    started = engine.request("START", source="tcp")
    assert started["ok"]
    path = Path(started["session_path"])
    for start in range(3123, len(data), 4096):
        assert engine.feed(AudioBlock(data[start:start+4096].tobytes(), start))
    engine.request("STOP", source="tcp")
    engine.close()
    meta, pcm = read_session(path)
    assert meta["status"] == "complete"
    assert meta["pretrigger_samples"] == 1460
    assert len(meta["segments"]) == 3
    assert pcm == data[3123-1460:].tobytes()
    events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
    assert events[0]["boundary_stream_sample"] == 3123
    assert events[0]["file_sample"] == 1460
    assert events[-1]["file_sample"] == len(pcm)//2


def tcp_command(port, cmd):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall((json.dumps(cmd) + "\n").encode())
        return json.loads(s.makefile("rb").readline())


def test_software_trigger_arming_idempotency_and_loopback(tmp_path):
    engine = RecorderEngine(config(tmp_path, pre_seconds=0))
    server = SoftwareTrigger(lambda: engine, 0)
    try:
        assert server.server.server_address[0] == "127.0.0.1"
        assert not tcp_command(server.port, {"command": "START"})["ok"]
        assert not tcp_command(server.port, {"command": "ARM"})["ok"]
        assert engine.request("ARM")["ok"]
        first = tcp_command(server.port, {"command": "START", "label": "trial_01"})
        second = tcp_command(server.port, {"command": "START"})
        assert first["session_path"] == second["session_path"]
        assert second["already_recording"]
        assert engine.feed(AudioBlock(b"\x34\x12"*6000, 0))
        assert tcp_command(server.port, {"command": "MARK", "label": "stimulus"})["ok"]
        assert tcp_command(server.port, {"command": "STOP"})["ok"]
        assert tcp_command(server.port, {"command": "STOP"})["ok"]
        assert not tcp_command(server.port, {"command": "MARK"})["ok"]
        engine.request("DISARM")
        assert not tcp_command(server.port, {"command": "START"})["ok"]
        meta, pcm = read_session(Path(first["session_path"]))
        assert meta["label"] == "trial_01"
        assert pcm == b"\x34\x12"*6000
        assert len(list(tmp_path.iterdir())) == 1
    finally:
        server.close()
        engine.close()


def test_discontinuity_closes_and_marks_incomplete(tmp_path):
    engine = RecorderEngine(config(tmp_path, pre_seconds=0))
    start = engine.request("START")
    engine.feed(AudioBlock(b"\x00\x01"*100, 0))
    engine.feed(AudioBlock(b"\x00\x01"*100, 101))
    wait_until(lambda: not engine.thread.is_alive())
    meta, pcm = read_session(Path(start["session_path"]))
    assert meta["status"] == "error"
    assert "discontinuity" in meta["errors"][0].lower()
    assert len(pcm) == 200
    assert not engine.request("START")["ok"]


def test_overflow_does_not_claim_success(tmp_path):
    engine = RecorderEngine(config(tmp_path, pre_seconds=0))
    result = engine.request("START")
    engine.feed(AudioBlock(b"\x01\x00"*1000, 0))
    engine.request("MARK", label="barrier")
    engine.fault("Audio input overflow")
    wait_until(lambda: not engine.thread.is_alive())
    meta, pcm = read_session(Path(result["session_path"]))
    assert meta["status"] == "error"
    assert len(pcm) == 2000
    assert engine.snapshot()["error"] == "Audio input overflow"


def test_short_trial_and_repeated_sessions(tmp_path):
    engine = RecorderEngine(config(tmp_path, pre_seconds=0))
    engine.request("ARM")
    cursor = 0
    for _ in range(12):
        assert engine.request("START", source="serial", device_us=1234)["ok"]
        engine.feed(AudioBlock(b"\x01\x00"*256, cursor))
        cursor += 256
        assert engine.request("STOP", source="serial", device_us=2345)["ok"]
    engine.close()
    sessions = list(tmp_path.iterdir())
    assert len(sessions) == 12
    assert all(read_session(path)[0]["recorded_samples"] == 256 for path in sessions)


def test_display_backpressure_does_not_change_recorded_audio(tmp_path):
    engine = RecorderEngine(config(tmp_path, pre_seconds=0))
    result = engine.request("START")
    for i in range(200):
        assert engine.feed(AudioBlock(b"\x01\x00"*100, i*100))
    engine.request("STOP")
    engine.close()
    _, pcm = read_session(Path(result["session_path"]))
    assert pcm == b"\x01\x00"*20_000
    assert engine.snapshot()["preview_drops"] > 0


def test_stft_frequency_and_amplitude_and_chunk_boundaries():
    rate, frequency = 200_000, 62500
    x = (.5*np.sin(2*np.pi*frequency*np.arange(40000)/rate)*32767).astype("<i2")
    stft = StreamingSTFT(nfft=512)
    parts = []
    times = []
    for start in range(0, len(x), 731):
        t, db = stft.feed(x[start:start+731].tobytes(), start)
        parts.append(db)
        times.extend(t)
    db = np.concatenate(parts)
    peaks = np.argmax(db, axis=1)*rate/512
    assert np.all(peaks == frequency)
    assert np.median(db.max(axis=1)) == pytest.approx(-6.021, abs=.03)
    assert np.allclose(np.diff(times), 128/rate)
    offline, step = file_spectrogram(x.astype(np.float32)/32768, rate, max_columns=100)
    assert len(offline) <= 100
    assert np.all(np.argmax(offline, axis=1)*rate/512 == frequency)


def test_mp3_is_audible_and_ten_times_longer(tmp_path):
    import shutil
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required only for independent verification, not for the app")
    engine = RecorderEngine(config(tmp_path, pre_seconds=0))
    start = engine.request("START")
    t = np.arange(40_000)/200_000
    x = (.25*np.sin(2*np.pi*60_000*t)*32767).astype("<i2")
    engine.feed(AudioBlock(x.tobytes(), 0))
    engine.request("STOP")
    engine.close()
    path = Path(start["session_path"])
    before = (path / "audio_0001.wav").read_bytes()
    result = export_session(path)
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(result), "-f", "f32le", "-ar", "44100", "-ac", "1", "pipe:1"], capture_output=True, check=True)
    audio = np.frombuffer(decoded.stdout, dtype="<f4")
    assert len(audio)/44100 == pytest.approx(2, abs=.12)
    middle = audio[len(audio)//4:3*len(audio)//4]
    peak = np.argmax(np.abs(np.fft.rfft(middle*np.hanning(len(middle)))))*44100/len(middle)
    assert peak == pytest.approx(6000, abs=3)
    assert (path / "audio_0001.wav").read_bytes() == before


def test_serial_real_byte_stream_via_pty(tmp_path):
    """Exercise serial framing over OS pseudo-terminal, including fragmented lines."""
    import os
    if os.name == "nt":
        pytest.skip("Pseudo-terminal is a POSIX integration fixture")
    import pty
    from ultramic_studio.triggers import SerialTrigger
    engine = RecorderEngine(config(tmp_path, pre_seconds=0))
    master, slave = pty.openpty()
    messages = []
    bridge = SerialTrigger(os.ttyname(slave), lambda: engine, messages.append)
    try:
        engine.request("ARM")
        os.write(master, b'{"command":"START",')
        time.sleep(.08)
        assert not engine.snapshot()["recording"]
        os.write(master, b'"device_us":12345}\n')
        wait_until(lambda: engine.snapshot()["recording"])
        path = Path(engine.snapshot()["session_path"])
        engine.feed(AudioBlock(b"\x02\x00"*500, 0))
        os.write(master, b'STOP\n')
        wait_until(lambda: not engine.snapshot()["recording"])
        meta, pcm = read_session(path)
        assert len(pcm) == 1000
        events = [json.loads(s) for s in (path/"events.jsonl").read_text().splitlines()]
        assert events[0]["device_us"] == 12345
    finally:
        bridge.close()
        engine.close()
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("line", ['{"command": 3}', 'ARM', '{"command":"START","label":4}',
                                  '{"command":"START","device_us":-1}', 'x'*1025])
def test_malformed_commands_are_rejected(line):
    with pytest.raises((ValueError, TypeError)):
        parse_command(line)


def test_simultaneous_duplicate_starts_create_one_recording(tmp_path):
    engine = RecorderEngine(config(tmp_path, pre_seconds=0))
    engine.request("ARM")
    results = []
    threads = [threading.Thread(target=lambda: results.append(engine.request("START", source="tcp"))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    engine.feed(AudioBlock(b"\x00\x00"*10, 0))
    engine.request("DISARM")
    engine.close()
    assert len(set(r["session_path"] for r in results)) == 1
    assert len(list(tmp_path.iterdir())) == 1
