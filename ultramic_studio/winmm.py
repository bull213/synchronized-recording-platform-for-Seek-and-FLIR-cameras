"""Direct Windows MME capture; no ACM format conversion or lower-rate retry.

The driver must accept mono PCM16 at 200 kHz with WAVE_FORMAT_DIRECT.
This does not independently verify the microphone's clock or frequency response.
"""
from __future__ import annotations

import ctypes as C
import platform
import threading
import time

from .core import AudioBlock

RATE, BLOCK, BUFFER_COUNT = 200_000, 2048, 32
WAVE_FORMAT_QUERY, WAVE_FORMAT_DIRECT = 0x0001, 0x0008
CALLBACK_EVENT = 0x00050000
WHDR_DONE, WHDR_PREPARED, WHDR_INQUEUE = 1, 2, 0x10
DWORD, WORD, HANDLE = C.c_uint32, C.c_uint16, C.c_void_p
# If a broken driver will not release memory, retain it until process exit.
_RETAINED_CAPTURES = []


class WaveFormat(C.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", WORD), ("nChannels", WORD),
                ("nSamplesPerSec", DWORD), ("nAvgBytesPerSec", DWORD),
                ("nBlockAlign", WORD), ("wBitsPerSample", WORD), ("cbSize", WORD)]


class WaveHeader(C.Structure):
    _fields_ = [("lpData", C.c_void_p), ("dwBufferLength", DWORD),
                ("dwBytesRecorded", DWORD), ("dwUser", C.c_size_t),
                ("dwFlags", DWORD), ("dwLoops", DWORD),
                ("lpNext", C.c_void_p), ("reserved", C.c_size_t)]


class WaveInCaps(C.Structure):
    # Windows WCHAR is 16 bits, including when tests run on a different OS.
    _fields_ = [("wMid", WORD), ("wPid", WORD), ("vDriverVersion", DWORD),
                ("szPname", WORD * 32), ("dwFormats", DWORD),
                ("wChannels", WORD), ("wReserved1", WORD)]

    @property
    def name(self):
        return bytes(self.szPname).decode("utf-16-le").split("\0", 1)[0]


class WinMM:
    """Typed Win32 calls, instantiated only on Windows."""
    def __init__(self):
        if platform.system() != "Windows":
            raise RuntimeError("Direct MME input is available only on Windows.")
        self.dll = C.WinDLL("winmm", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "waveInGetNumDevs": ([], DWORD),
            "waveInGetDevCapsW": ([C.c_size_t, C.POINTER(WaveInCaps), DWORD], DWORD),
            "waveInGetErrorTextW": ([DWORD, C.c_wchar_p, DWORD], DWORD),
            "waveInOpen": ([C.POINTER(HANDLE), C.c_uint32, C.POINTER(WaveFormat),
                            C.c_size_t, C.c_size_t, DWORD], DWORD),
        }
        for name in ("waveInPrepareHeader", "waveInAddBuffer", "waveInUnprepareHeader"):
            signatures[name] = ([HANDLE, C.POINTER(WaveHeader), DWORD], DWORD)
        for name in ("waveInStart", "waveInReset", "waveInClose"):
            signatures[name] = ([HANDLE], DWORD)
        for name, (args, result) in signatures.items():
            fn = getattr(self.dll, name)
            fn.argtypes, fn.restype = args, result
            setattr(self, name, fn)
        for name, args, result in (
            ("CreateEventW", [C.c_void_p, C.c_int32, C.c_int32, C.c_wchar_p], HANDLE),
            ("SetEvent", [HANDLE], C.c_int32),
            ("WaitForSingleObject", [HANDLE, DWORD], DWORD),
            ("CloseHandle", [HANDLE], C.c_int32),
        ):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
            setattr(self, name, fn)

    def check(self, code, operation):
        if code:
            message = C.create_unicode_buffer(256)
            self.waveInGetErrorTextW(code, message, len(message))
            raise RuntimeError(f"{operation}: {message.value or 'Windows audio error'} (MME {code}).")

    def caps(self, device_id):
        caps = WaveInCaps()
        self.check(self.waveInGetDevCapsW(device_id, C.byref(caps), C.sizeof(caps)),
                   "Read MME microphone")
        return caps

    def devices(self):
        result = []
        for device_id in range(self.waveInGetNumDevs()):
            caps = self.caps(device_id)
            if caps.wChannels:
                result.append({"name": caps.name, "host_api": "MME",
                               "backend": "winmm_direct", "mme_device_id": device_id,
                               "max_input_channels": caps.wChannels,
                               "default_samplerate": None})
        return result


class MMESource:
    def __init__(self, engine, device, *, api=None):
        self.engine, self.device, self.api = engine, device, api
        self.handle = HANDLE()
        self.event = None
        self.buffers, self.headers = [], []
        self.cursor = self.next_buffer = 0
        self.stop_event = threading.Event()
        self.thread = None
        self.last_callback_ns = time.perf_counter_ns()

    def start(self):
        self.api = self.api or WinMM()
        api = self.api
        device_id = self.device["mme_device_id"]
        if api.caps(device_id).name != self.device["name"]:
            raise RuntimeError("The MME device list changed. Refresh devices and select the Ultramic again.")
        fmt = WaveFormat(1, 1, RATE, RATE * 2, 2, 16, 0)
        api.check(api.waveInOpen(None, device_id, C.byref(fmt), 0, 0,
                                WAVE_FORMAT_QUERY | WAVE_FORMAT_DIRECT),
                  "Microphone must support 200000 Hz, mono, 16-bit without ACM conversion")
        self.event = api.CreateEventW(None, False, False, None)
        if not self.event:
            raise RuntimeError("Windows could not create the microphone notification event.")
        try:
            api.check(api.waveInOpen(C.byref(self.handle), device_id, C.byref(fmt), self.event,
                                    0, CALLBACK_EVENT | WAVE_FORMAT_DIRECT), "Open MME microphone")
            for _ in range(BUFFER_COUNT):
                buffer = C.create_string_buffer(BLOCK * 2)
                header = WaveHeader(lpData=C.addressof(buffer), dwBufferLength=BLOCK * 2)
                # Keep both objects alive while the driver owns their addresses.
                self.buffers.append(buffer)
                self.headers.append(header)
                api.check(api.waveInPrepareHeader(self.handle, C.byref(header), C.sizeof(header)),
                          "Prepare MME buffer")
                api.check(api.waveInAddBuffer(self.handle, C.byref(header), C.sizeof(header)),
                          "Queue MME buffer")
            self.engine.config.device.update(
                backend="winmm_direct", requested_rate_hz=RATE, accepted_format_rate_hz=RATE,
                requested_block_frames=BLOCK, buffer_count=BUFFER_COUNT,
                acm_conversion_disabled=True, driver_timestamps_available=False,
                capture_note="Driver accepted PCM16 mono 200 kHz with WAVE_FORMAT_DIRECT; "
                             "physical sampling rate and ultrasonic response are not independently measured.")
            api.check(api.waveInStart(self.handle), "Start MME microphone")
            self.last_callback_ns = time.perf_counter_ns()
            self.thread = threading.Thread(target=self._run, name="MME microphone", daemon=True)
            self.thread.start()
        except Exception:
            self._release(flush=False)
            raise

    def _drain(self, requeue=True):
        # At most one pass per wakeup. Process in submission order, not event count;
        # multiple completed buffers can share one Windows event notification.
        for _ in range(len(self.headers)):
            if requeue and all(h.dwFlags & WHDR_DONE for h in self.headers):
                raise RuntimeError("All MME buffers filled before processing. Possible audio loss; "
                                   "capture stopped and the recording is marked incomplete.")
            header = self.headers[self.next_buffer]
            if not header.dwFlags & WHDR_DONE:
                break
            size = header.dwBytesRecorded
            if size > header.dwBufferLength or size % 2:
                raise RuntimeError("The MME driver returned an invalid PCM buffer size.")
            if requeue and size != header.dwBufferLength:
                raise RuntimeError("The MME driver returned a short buffer unexpectedly. Check the USB microphone.")
            if size:
                self.last_callback_ns = time.perf_counter_ns()
                block = AudioBlock(C.string_at(header.lpData, size), self.cursor,
                                   received_ns=self.last_callback_ns)
                if not self.engine.feed(block):
                    return False
                self.cursor += block.frames
            if requeue:
                if all(h.dwFlags & WHDR_DONE for h in self.headers):
                    raise RuntimeError("All MME buffers filled before recycling. Possible audio loss; "
                                       "the recording is marked incomplete.")
                header.dwBytesRecorded = 0
                self.api.check(self.api.waveInAddBuffer(self.handle, C.byref(header), C.sizeof(header)),
                               "Recycle MME buffer")
            self.next_buffer = (self.next_buffer + 1) % len(self.headers)
        return True

    def _run(self):
        flush = False
        try:
            while not self.stop_event.is_set():
                result = self.api.WaitForSingleObject(self.event, 100)
                if result not in (0, 0x102):
                    raise RuntimeError(f"Windows microphone wait failed ({result}).")
                if self.stop_event.is_set():
                    break
                if not self._drain():
                    return
                if time.perf_counter_ns() - self.last_callback_ns > 3_000_000_000:
                    raise RuntimeError("No audio has arrived for 3 seconds. Check the USB microphone.")
            flush = True
        except Exception as exc:
            self.engine.fault(str(exc))
        finally:
            self._release(flush=flush)

    def _release(self, flush):
        errors = []
        if self.handle.value:
            if flush and self.headers and all(h.dwFlags & WHDR_DONE for h in self.headers):
                self.engine.fault("All MME buffers filled before stopping. Possible audio loss; "
                                  "the recording is marked incomplete.")
                flush = False
            reset = self.api.waveInReset(self.handle)
            if reset:
                errors.append(f"reset {reset}")
            elif flush:
                try:
                    self._drain(requeue=False)
                except Exception as exc:
                    self.engine.fault(str(exc))
            for header in self.headers:
                if header.dwFlags & WHDR_PREPARED:
                    code = self.api.waveInUnprepareHeader(self.handle, C.byref(header), C.sizeof(header))
                    if code:
                        errors.append(f"release buffer {code}")
            code = self.api.waveInClose(self.handle)
            if code:
                errors.append(f"close {code}")
            else:
                self.handle = HANDLE()
        if errors:
            # Do not free buffers or close an event that a faulty driver might use.
            _RETAINED_CAPTURES.append(self)
            self.engine.fault("Windows could not release the microphone: " + ", ".join(errors) +
                              ". Close and restart the app before reconnecting.")
        elif self.event:
            self.api.CloseHandle(self.event)
            self.event = None

    def check_health(self):
        if self.thread and not self.thread.is_alive() and not self.stop_event.is_set():
            self.engine.fault("The MME microphone stopped unexpectedly. Reconnect the microphone.")

    def stop(self):
        self.stop_event.set()
        # Wait is bounded at 100 ms; avoid racing SetEvent with the worker's close.
        if self.thread and self.thread.ident is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                self.engine.fault("The Windows microphone driver did not stop. Restart the app before reconnecting.")
