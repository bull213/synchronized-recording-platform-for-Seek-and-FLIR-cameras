import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ultramic_studio.core import AudioBlock, RecorderEngine, RecordingConfig


def wait_for(check, seconds=5):
    until = time.monotonic()+seconds
    while time.monotonic() < until and not check():
        time.sleep(.01)
    assert check()


def test_recording_queue_saturation_is_fatal(tmp_path):
    import threading
    engine = RecorderEngine(RecordingConfig(output=tmp_path, auto_mp3=False, pre_seconds=0), queue_blocks=1)
    started = engine.request("START")
    entered, release = threading.Event(), threading.Event()
    original = engine._audio
    def slow(block):
        entered.set()
        release.wait(2)
        return original(block)
    engine._audio = slow
    assert engine.feed(AudioBlock(b"\x01\x00"*100, 0))
    assert entered.wait(2)
    assert engine.feed(AudioBlock(b"\x01\x00"*100, 100))
    assert not engine.feed(AudioBlock(b"\x01\x00"*100, 200))
    release.set()
    wait_for(lambda: not engine.thread.is_alive())
    metadata = json.loads((Path(started["session_path"])/"session.json").read_text())
    assert metadata["status"] == "error"
    assert "overflow" in metadata["errors"][0]


def test_disk_space_failure_is_reported_before_recording(tmp_path, monkeypatch):
    import ultramic_studio.core as core
    monkeypatch.setattr(core.shutil, "disk_usage", lambda path: SimpleNamespace(free=10))
    engine = RecorderEngine(RecordingConfig(output=tmp_path, auto_mp3=False))
    result = engine.request("START")
    assert not result["ok"]
    assert "100 MB" in result["error"]
    engine.close()
    assert not list(tmp_path.iterdir())


def test_export_failure_retains_original_wav_and_cleans_partial(tmp_path, monkeypatch):
    import lameenc
    from ultramic_studio.export import export_session
    engine = RecorderEngine(RecordingConfig(output=tmp_path, auto_mp3=False, pre_seconds=0))
    result = engine.request("START")
    engine.feed(AudioBlock(b"\x01\x00"*1000, 0))
    engine.request("STOP")
    engine.close()
    folder = Path(result["session_path"])
    before = (folder/"audio_0001.wav").read_bytes()
    class BrokenEncoder:
        def __getattr__(self, name):
            return lambda *args: None
        def encode(self, pcm):
            raise OSError("Simulated encoder failure")
    monkeypatch.setattr(lameenc, "Encoder", BrokenEncoder)
    with pytest.raises(OSError):
        export_session(folder)
    assert (folder/"audio_0001.wav").read_bytes() == before
    assert not list(folder.glob("*.partial"))
    assert not (folder/"listening_10x.mp3").exists()


def test_native_source_refuses_rate_mismatch(tmp_path, monkeypatch):
    """A driver-reported mismatch must never be accepted as 200 kHz."""
    import sys
    import ultramic_studio.audio as audio
    class WrongRate:
        samplerate = 48000
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def close(self):
            pass
    fake = SimpleNamespace(check_input_settings=lambda **kwargs: None, RawInputStream=WrongRate)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(audio, "native_settings", lambda device: None)
    engine = RecorderEngine(RecordingConfig(output=tmp_path, auto_mp3=False))
    source = audio.NativeSource(engine, {"index": 0})
    with pytest.raises(RuntimeError, match="48000"):
        source.start()
    engine.close()


def test_gui_record_trigger_export_and_review(tmp_path, monkeypatch):
    """End-to-end UI state, demo acquisition, background MP3, and file review."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from ultramic_studio.gui import MainWindow
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    application.setStyle("Fusion")
    window = MainWindow()
    def fail(message):
        raise AssertionError(message)
    monkeypatch.setattr(window, "error", fail)
    window.output.setText(str(tmp_path))
    window.mode.setCurrentIndex(1)
    window.show()
    def pump_until(check, seconds=10):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            application.processEvents()
            if check():
                return
            time.sleep(.01)
        assert check()
    try:
        window.toggle_connection()
        window.arm.setChecked(True)
        assert window.engine.request("START", source="tcp")["ok"]
        pump_until(lambda: window.engine.snapshot()["recorded_samples"] > 30_000)
        assert window.engine.request("STOP", source="tcp")["ok"]
        pump_until(lambda: window.last_session is not None)
        pump_until(lambda: not window.exports)
        assert (window.last_session / "listening_10x.mp3").stat().st_size > 1000
        assert window.live.image.image is not None
        window.set_review_file(window.last_session / "audio_0001.wav")
        pump_until(lambda: window.review_future is None)
        assert window.review.image.image is not None
        assert window.review.image.image.shape[0] == 257
        assert not window.engine.snapshot()["error"]
    finally:
        window.close()
        application.processEvents()
