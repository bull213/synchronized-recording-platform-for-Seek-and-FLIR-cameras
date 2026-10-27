"""Native 200 kHz input and clearly labelled synthetic demo input."""
from __future__ import annotations

import platform
import threading
import time

import numpy as np

from .core import AudioBlock, RecorderEngine


RATE = 200_000
BLOCK = 2048


def list_devices() -> list[dict]:
    result = []
    windows = platform.system() == "Windows"
    if windows:
        from .winmm import WinMM
        mme_devices = WinMM().devices()
    else:
        mme_devices = []
    try:
        import sounddevice as sd
        hosts = sd.query_hostapis()
        pa_devices = sd.query_devices()
    except Exception:
        if not mme_devices:
            raise
        hosts, pa_devices = [], []
    for i, item in enumerate(pa_devices):
        host = hosts[item["hostapi"]]["name"]
        if item["max_input_channels"] > 0 and not (windows and host == "MME"):
            result.append({"index": i, "name": item["name"],
                           "host_api": host,
                           "default_samplerate": item["default_samplerate"],
                           "max_input_channels": item["max_input_channels"]})
    # These indices are app-local, never passed as PortAudio device IDs.
    # Enumerate WinMM IDs directly: PortAudio's MME list includes a mapper entry.
    for offset, device in enumerate(mme_devices):
        result.append(dict(device, index=len(pa_devices) + offset))
    def rank(d):
        named = "ultra" in d["name"].lower() or "dodotronic" in d["name"].lower()
        native = "WASAPI" in d["host_api"] or "WDM-KS" in d["host_api"]
        return (not named, not native, d["index"])
    return sorted(result, key=rank)


def create_input_source(engine: RecorderEngine, device: dict):
    if device.get("backend") == "winmm_direct":
        from .winmm import MMESource
        return MMESource(engine, device)
    return NativeSource(engine, device)


def native_settings(device: dict):
    import sounddevice as sd
    host = device["host_api"]
    if "WASAPI" in host:
        return sd.WasapiSettings(exclusive=True, auto_convert=False)
    if "Core Audio" in host:
        return sd.CoreAudioSettings(change_device_parameters=True, fail_if_conversion_required=True)
    if platform.system() == "Windows" and not any(x in host for x in ("WDM-KS", "ASIO")):
        raise RuntimeError("Refresh devices and select the microphone's MME, WASAPI, or WDM-KS entry. "
                           "MME uses the app's direct Windows capture path; DirectSound is disabled.")
    if platform.system() == "Linux" and not ("hw:" in device["name"] and "ALSA" in host):
        raise RuntimeError("Select a direct ALSA hardware input containing 'hw:'. "
                           "Desktop audio mixers may resample ultrasound.")
    return None


class NativeSource:
    def __init__(self, engine: RecorderEngine, device: dict):
        self.engine, self.device = engine, device
        self.stream = None
        self.cursor = 0
        self.stopping = False
        self.started_ns = self.last_callback_ns = time.perf_counter_ns()

    def start(self) -> None:
        import sounddevice as sd
        extra = native_settings(self.device)
        sd.check_input_settings(device=self.device["index"], samplerate=RATE,
                                channels=1, dtype="int16", extra_settings=extra)
        try:
            self.stream = sd.RawInputStream(
                device=self.device["index"], samplerate=RATE, channels=1,
                dtype="int16", blocksize=BLOCK, latency="low", extra_settings=extra,
                callback=self._callback, finished_callback=self._finished,
            )
            if abs(self.stream.samplerate - RATE) > 0.5:
                raise RuntimeError(f"Driver returned {self.stream.samplerate} Hz, not 200000 Hz.")
            self.engine.config.device.update(
                requested_rate_hz=RATE, driver_reported_rate_hz=self.stream.samplerate,
                driver_reported_input_latency_seconds=self.stream.latency,
                requested_block_frames=BLOCK, native_mode=True)
            self.stream.start()
        except Exception:
            if self.stream is not None:
                self.stopping = True
                self.stream.close()
                self.stream = None
            raise

    def _callback(self, data, frames, timing, status) -> None:
        import sounddevice as sd
        self.last_callback_ns = time.perf_counter_ns()
        if status:
            self.engine.fault(f"Audio driver reported {status}. Capture stopped to flag incomplete data.")
            raise sd.CallbackAbort
        block = AudioBlock(bytes(data), self.cursor, float(timing.inputBufferAdcTime),
                           float(timing.currentTime), self.last_callback_ns)
        self.cursor += frames
        if not self.engine.feed(block):
            raise sd.CallbackAbort

    def _finished(self) -> None:
        if not self.stopping:
            self.engine.fault("The audio device stopped unexpectedly or was disconnected.")

    def check_health(self) -> None:
        if not self.stopping and time.perf_counter_ns() - self.last_callback_ns > 3_000_000_000:
            self.engine.fault("No audio has arrived for 3 seconds. Check the USB microphone.")

    def stop(self) -> None:
        self.stopping = True
        if self.stream is not None:
            try:
                self.stream.stop()
            finally:
                self.stream.close()
                self.stream = None


def synthetic_pcm(start: int, frames: int, rng: np.random.Generator) -> bytes:
    """Synthetic chirps at 42–83 kHz; this is not a mouse dataset."""
    t = np.arange(start, start + frames, dtype=np.float64) / RATE
    x = rng.normal(0, 0.0014, frames)
    cycle = t % 2.4
    for onset, duration, f0, f1 in [(.12, .075, 48000, 68000), (.30, .11, 78000, 56000),
                                    (.56, .09, 57000, 58000), (.75, .065, 65000, 82000),
                                    (1.30, .15, 42000, 59000), (1.70, .08, 70000, 52000)]:
        u = cycle - onset
        valid = (u >= 0) & (u < duration)
        v = u[valid]
        phase = 2 * np.pi * (f0 * v + .5 * (f1 - f0) / duration * v * v)
        x[valid] += .14 * np.sin(np.pi * v / duration) ** 2 * np.sin(phase)
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


class DemoSource:
    def __init__(self, engine: RecorderEngine):
        self.engine = engine
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="Synthetic audio", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        rng = np.random.default_rng(42)
        cursor = 0
        origin = time.monotonic()
        while not self.stop_event.is_set():
            block = AudioBlock(synthetic_pcm(cursor, BLOCK, rng), cursor)
            if not self.engine.feed(block):
                break
            cursor += BLOCK
            self.stop_event.wait(max(0, origin + cursor / RATE - time.monotonic()))

    def check_health(self) -> None:
        pass

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)
