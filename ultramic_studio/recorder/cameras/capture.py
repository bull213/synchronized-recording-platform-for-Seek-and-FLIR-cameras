"""Seek callbacks and FLIR acquisition workers; SDKs load only on connection."""
import ctypes
import math
import os
import queue
import sys
import threading
import time
from ctypes import (c_void_p, c_int, c_size_t, c_uint32, c_uint8,
                    c_uint16, c_float, c_char, c_char_p, c_uint64, POINTER, CFUNCTYPE)

import numpy as np
import cv2
PySpin = None
from ultramic_studio.recorder.cameras.runtime import find_seek_dll, read_flir_frame, start_flir_cameras
from ultramic_studio.recorder.reliability import log

# ----------------- CONFIGURATION -----------------
SEEK_DLL_PATH = SEEK_DLL_DIR = None

SEEK_IO_TYPE_USB = 1
SEEK_EVENT_CONNECT = 0
SEEK_EVENT_DISCONNECT = 1
SEEK_EVENT_ERROR = 2
SEEK_EVENT_READY_TO_PAIR = 3

# Frame format bitmasks
SEEK_FRAME_FORMAT_CORRECTED          = 0x04
SEEK_FRAME_FORMAT_THERMOGRAPHY_FLOAT = 0x10
SEEK_FRAME_FORMAT_COLOR_ARGB8888     = 0x80

SEEK_W, SEEK_H = 320, 240
SCALE_FACTOR = 2  # Feature 7: 2x upscaling matching C++ SDL2 implementation

SEEK_WINDOW = "Seek Thermal (live)"

# ----------------- LOAD SEEK DLL -----------------
seek = None
_seek_signatures = []


def _load_seek():
    global seek, SEEK_DLL_PATH, SEEK_DLL_DIR, _seek_dll_directory
    if seek is not None:
        return
    SEEK_DLL_PATH = str(find_seek_dll())
    SEEK_DLL_DIR = os.path.dirname(SEEK_DLL_PATH)
    try:
        _seek_dll_directory = os.add_dll_directory(SEEK_DLL_DIR)
    except (AttributeError, FileNotFoundError):
        pass
    library = ctypes.CDLL(SEEK_DLL_PATH)
    for name, argtypes, restype in _seek_signatures:
        function = getattr(library, name)
        function.argtypes = argtypes
        function.restype = restype
    seek = library
    log.info('Seek library: %s', SEEK_DLL_PATH)
    print('[Seek] DLL loaded from:', SEEK_DLL_PATH)

FRAME_CALLBACK = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p)
EVENT_CALLBACK = CFUNCTYPE(None, c_void_p, c_int, c_int, c_void_p)

def sig(name, argtypes, restype=c_int):
    _seek_signatures.append((name, argtypes, restype))

sig('seekcamera_error_get_str', [c_int], c_char_p)
sig('seekcamera_manager_get_event_str', [c_int], c_char_p)
sig('seekcamera_manager_create', [POINTER(c_void_p), c_int])
sig('seekcamera_manager_destroy', [POINTER(c_void_p)])
sig('seekcamera_manager_register_event_callback', [c_void_p, EVENT_CALLBACK, c_void_p])
sig('seekcamera_register_frame_available_callback', [c_void_p, FRAME_CALLBACK, c_void_p])
sig('seekcamera_capture_session_start', [c_void_p, c_uint32])
sig('seekcamera_capture_session_stop', [c_void_p])
sig('seekcamera_frame_lock', [c_void_p])
sig('seekcamera_frame_unlock', [c_void_p])
sig('seekframe_get_width',  [c_void_p], c_size_t)
sig('seekframe_get_height', [c_void_p], c_size_t)
sig('seekframe_get_data',   [c_void_p], c_void_p)
sig('seekframe_get_channels',    [c_void_p], c_size_t)
sig('seekframe_get_pixel_depth', [c_void_p], c_size_t)
sig('seekframe_get_line_stride', [c_void_p], c_size_t)
sig('seekframe_get_data_size',   [c_void_p], c_size_t)
sig('seekframe_get_header', [c_void_p], c_void_p)
sig('seekcamera_frame_get_frame_by_format', [c_void_p, c_uint32, POINTER(c_void_p)])
sig('seekcamera_get_chipid', [c_void_p, POINTER(c_char * 16)])

SEEKCAMERA_SHUTTER_MODE_AUTO = 0
SEEKCAMERA_SHUTTER_MODE_MANUAL = 1
SEEKCAMERA_FILTER_SHARPEN_CORRECTION = 2
SEEKCAMERA_COLOR_PALETTE_USER_0 = 11

sig('seekcamera_set_shutter_mode', [c_void_p, c_int])
sig('seekcamera_shutter_trigger',  [c_void_p])
sig('seekcamera_get_color_palette', [c_void_p, POINTER(c_int)])
sig('seekcamera_set_color_palette', [c_void_p, c_int])
sig('seekcamera_color_palette_get_str', [c_int], c_char_p)
sig('seekcamera_get_pipeline_mode', [c_void_p, POINTER(c_int)])
sig('seekcamera_set_pipeline_mode', [c_void_p, c_int])
sig('seekcamera_pipeline_mode_get_str', [c_int], c_char_p)
sig('seekcamera_get_filter_state', [c_void_p, c_int, POINTER(c_int)])
sig('seekcamera_set_filter_state', [c_void_p, c_int, c_int])

# Feature 4: Full Header mapping from seekcamera-simple.cpp
class SeekFrameHeader(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("sentinel", c_uint32),
        ("version", c_uint8),
        ("type", c_uint32),
        ("width", c_uint16),
        ("height", c_uint16),
        ("channels", c_uint8),
        ("pixel_depth", c_uint8),
        ("pixel_padding", c_uint8),
        ("line_stride", c_uint16),
        ("line_padding", c_uint16),
        ("header_size", c_uint16),
        ("timestamp_utc_ns", c_uint64),
        ("chipid", c_char * 16),
        ("serial_number", c_char * 16),
        ("core_part_number", c_char * 32),
        ("firmware_version", c_uint8 * 4),
        ("io_type", c_uint8),
        ("fpa_frame_count", c_uint32),
        ("fpa_diode_count", c_uint32),
        ("environment_temperature", c_float),
        ("thermography_min_x", c_uint16),
        ("thermography_min_y", c_uint16),
        ("thermography_min_value", c_float),
        ("thermography_max_x", c_uint16),
        ("thermography_max_y", c_uint16),
        ("thermography_max_value", c_float),
        ("thermography_spot_x", c_uint16),
        ("thermography_spot_y", c_uint16),
        ("thermography_spot_value", c_float),
        ("agc_mode", c_uint8),
        ("histeq_agc_num_bins", c_uint16),
        ("histeq_agc_bin_width", c_uint16),
        ("histeq_agc_gain_limit_factor", c_float),
        ("histeq_agc_reserved", c_uint16 * 32),
        ("linear_agc_min", c_float),
        ("linear_agc_max", c_float),
        ("linear_agc_reserved", c_float * 32),
        ("gradient_correction_filter_state", c_uint8),
        ("flat_scene_correction_filter_state", c_uint8),
    ]

# ----------------- SHARED STATE -----------------
recording = threading.Event()
coordinator = None
stop_all = threading.Event()
shutter_config_lock = threading.Lock()
seek_control_lock = threading.RLock()
seek_events = queue.Queue(maxsize=256)
seek_event_worker = None
seek_session_generation = 0
seek_chipid = None
seek_preferences = {}
ui_events = queue.Queue()
seek_sample_mode = "custom"
seek_sample_hz = 1.0
latest_ir_lock = threading.Lock()
latest_ir = None
active_seek_cam = None
seek_connected = threading.Event()
first_seek_frame = threading.Event()
_diag_printed = False
_thermal_diag_printed = False

# Per-camera FLIR preview state, keyed by camera index.
flir_preview_lock = threading.Lock()
flir_previews = {}   # {cam_index: np.ndarray}

shutter_config = {"mode": SEEKCAMERA_SHUTTER_MODE_AUTO, "timed": False, "hz": 0.0}


def configure_seek_shutter(mode, timed=False, hz=0.0):
    """Apply automatic Seek shuttering or manual shutter triggers at hz."""
    if mode == SEEKCAMERA_SHUTTER_MODE_AUTO:
        timed, hz = False, 0.0
    elif mode == SEEKCAMERA_SHUTTER_MODE_MANUAL:
        hz = float(hz) if timed else 0.0
        if timed and hz <= 0:
            raise ValueError("Custom shutter rate must be greater than zero Hz.")
    else:
        raise ValueError("Unsupported Seek shutter mode.")

    with seek_control_lock:
        cam = active_seek_cam
        if cam:
            result = seek.seekcamera_set_shutter_mode(cam, mode)
            if result != 0:
                raise RuntimeError(f"The camera rejected the shutter setting (error {result}).")
        with shutter_config_lock:
            shutter_config.update(mode=mode, timed=timed, hz=hz)

# ----------------- SEEK CALLBACKS -----------------
def _read_seek_frame(cam_ptr, frame_ptr, user_data):
    global latest_ir, _diag_printed, _thermal_diag_printed
    received_ns = time.perf_counter_ns()
    generation = (user_data.value if isinstance(user_data, c_void_p) else user_data or 1) - 1
    if not frame_ptr or generation != seek_session_generation:
        return
    snap = None
    failure = None
    seek.seekcamera_frame_lock(frame_ptr)
    try:
        # Extract Color Preview
        sub_color = c_void_p()
        res_color = seek.seekcamera_frame_get_frame_by_format(
            frame_ptr, SEEK_FRAME_FORMAT_COLOR_ARGB8888, ctypes.byref(sub_color))

        arr = None
        if res_color == 0 and sub_color:
            w = seek.seekframe_get_width(sub_color)
            h = seek.seekframe_get_height(sub_color)
            d = seek.seekframe_get_data(sub_color)
            stride = seek.seekframe_get_line_stride(sub_color)
            dsize = seek.seekframe_get_data_size(sub_color)

            if (w, h) == (SEEK_W, SEEK_H) and d:
                buf = np.ctypeslib.as_array(ctypes.cast(d, POINTER(ctypes.c_uint8)), shape=(dsize,))
                arr = buf.reshape(h, stride)[:, :w * 4].reshape(h, w, 4)

        # Request calibrated temperature values (°C), not corrected 16-bit
        # sensor counts. The corrected frame is not interchangeable with the
        # thermography-float frame used by the reference CSV.
        sub_raw = c_void_p()
        res_raw = seek.seekcamera_frame_get_frame_by_format(
            frame_ptr, SEEK_FRAME_FORMAT_THERMOGRAPHY_FLOAT, ctypes.byref(sub_raw))

        if not _diag_printed:
            err_ptr = seek.seekcamera_error_get_str(res_raw)
            err_str = ctypes.string_at(err_ptr).decode('utf-8', 'ignore') if err_ptr else "?"
            print(f"[Seek DIAG] thermography-float request: res={res_raw} ({err_str}), "
                  f"sub_raw_ptr={sub_raw.value}")
            _diag_printed = True

        raw_data = None
        header_data = None
        wt = ht = deptht = stride = dsizet = channels = 0

        if res_raw == 0 and sub_raw:
            wt = seek.seekframe_get_width(sub_raw)
            ht = seek.seekframe_get_height(sub_raw)
            dt = seek.seekframe_get_data(sub_raw)
            dsizet = seek.seekframe_get_data_size(sub_raw)
            deptht = seek.seekframe_get_pixel_depth(sub_raw)

            # seekframe_get_pixel_depth returns bits per pixel (e.g. 32), not
            # bytes per pixel. Thermography-float pixels are temperatures in
            # °C. Respect row stride because data_size can include row padding.
            stride = seek.seekframe_get_line_stride(sub_raw)
            channels = seek.seekframe_get_channels(sub_raw)
            # This path intentionally accepts only the 32-bit float format;
            # writing a 16-bit corrected frame here would produce counts.
            dtype = np.dtype(np.float32) if deptht == 32 else None
            if dt and dtype is not None and channels == 1 and wt > 0 and ht > 0:
                row_bytes = wt * dtype.itemsize
                required_size = stride * (ht - 1) + row_bytes
                if stride >= row_bytes and dsizet >= required_size:
                    t_buf = np.ctypeslib.as_array(
                        ctypes.cast(dt, POINTER(c_uint8)), shape=(dsizet,))
                    raw_data = np.ndarray(
                        shape=(ht, wt), dtype=dtype, buffer=t_buf,
                        strides=(stride, dtype.itemsize)).copy()

            header_ptr = seek.seekframe_get_header(sub_raw)
            if header_ptr:
                header = ctypes.cast(header_ptr, POINTER(SeekFrameHeader)).contents
                header_data = {}
                for field in SeekFrameHeader._fields_:
                    k = field[0]
                    v = getattr(header, k)
                    if isinstance(v, bytes):
                        v = v.decode('utf-8', 'ignore').strip('\x00')
                    elif isinstance(v, ctypes.Array):
                        v = list(v)
                    header_data[k] = v

        if arr is not None:
            if raw_data is None and not _thermal_diag_printed:
                print(f"[Seek DIAG] thermal extraction failed: result={res_raw}, "
                      f"frame={wt}x{ht}, pixel_depth={deptht} bits, "
                      f"channels={channels}, stride={stride}, data_size={dsizet}")
                _thermal_diag_printed = True
            snap = (time.time(), arr.copy(), raw_data, header_data, received_ns)
    except Exception as exc:
        failure = str(exc)
    finally:
        seek.seekcamera_frame_unlock(frame_ptr)
    # SDK buffers are unlocked before calling application code or taking its lock.
    if generation != seek_session_generation:
        return
    if failure:
        if coordinator:
            coordinator.camera_fault('seek', f'Seek frame extraction: {failure}', generation=generation)
        return
    if snap is not None:
        with latest_ir_lock:
            latest_ir = snap
        if coordinator is not None:
            coordinator.on_seek(snap, generation=generation)
        if snap[2] is not None and snap[3] is not None:
            if not first_seek_frame.is_set():
                log.info('Seek first valid frame: identity=%s firmware=%s generation=%s',
                         seek_chipid, snap[3].get('firmware_version'), generation)
            first_seek_frame.set()


def on_frame_available(cam_ptr, frame_ptr, user_data):
    # Never let a Python exception escape the ctypes callback boundary.
    try:
        _read_seek_frame(cam_ptr, frame_ptr, user_data)
    except Exception as exc:
        log.exception('Seek frame callback failed')
        if coordinator:
            generation = (user_data.value if isinstance(user_data, c_void_p) else user_data or 1) - 1
            coordinator.camera_fault('seek', f'Seek frame callback: {exc}', generation=generation)


def on_camera_event(cam_ptr, event_type, event_status, user_data):
    generation = (user_data.value if isinstance(user_data, c_void_p) else user_data or 1) - 1
    try:
        seek_events.put_nowait((cam_ptr, event_type, event_status, generation))
    except queue.Full:
        log.warning('Seek device-event queue full; frame watchdog remains active.')


def _handle_seek_event(cam_ptr, event_type, event_status, generation):
    global active_seek_cam, seek_chipid
    failure = None
    disconnected = False
    with seek_control_lock:
        if generation != seek_session_generation:
            return
        if event_type == SEEK_EVENT_CONNECT:
            cid_buf = (c_char * 16)()
            result = seek.seekcamera_get_chipid(cam_ptr, ctypes.byref(cid_buf))
            if result != 0:
                raise RuntimeError(f'Could not identify Seek camera (error {result}).')
            cid = cid_buf.value.decode('utf-8', 'ignore')
            if seek_chipid is not None and cid != seek_chipid:
                log.warning('Ignoring different Seek camera %s; expected %s', cid, seek_chipid)
                return
            seek_chipid = cid
            active_seek_cam = cam_ptr
            with shutter_config_lock:
                mode = shutter_config['mode']
            for configure in (
                lambda: seek.seekcamera_set_shutter_mode(cam_ptr, mode),
                lambda: seek.seekcamera_register_frame_available_callback(cam_ptr, c_frame_cb, c_void_p(generation + 1)),
            ):
                result = configure()
                if result != 0:
                    raise RuntimeError(f'Seek setup failed (error {result}).')
            result = seek.seekcamera_capture_session_start(
                cam_ptr, SEEK_FRAME_FORMAT_COLOR_ARGB8888 | SEEK_FRAME_FORMAT_THERMOGRAPHY_FLOAT)
            if result != 0:
                raise RuntimeError(f'Seek capture start failed (error {result}).')
            for setting in ('pipeline', 'palette', 'sharpen'):
                if setting not in seek_preferences:
                    continue
                value = seek_preferences[setting]
                if setting == 'pipeline':
                    result = seek.seekcamera_set_pipeline_mode(cam_ptr, value)
                elif setting == 'palette':
                    result = seek.seekcamera_set_color_palette(cam_ptr, value)
                else:
                    result = seek.seekcamera_set_filter_state(cam_ptr, SEEKCAMERA_FILTER_SHARPEN_CORRECTION, value)
                if result != 0:
                    raise RuntimeError(f'Seek could not restore {setting} (error {result}).')
            seek_connected.set()
            log.info('Seek connected: %s generation=%s', cid, generation)
            ui_events.put(('seek_event', event_type, 'connected', cid))
        elif event_type == SEEK_EVENT_DISCONNECT and cam_ptr == active_seek_cam:
            active_seek_cam = None
            seek_connected.clear()
            failure, disconnected = 'Seek disconnected.', True
        elif event_type == SEEK_EVENT_ERROR and cam_ptr == active_seek_cam:
            error = seek.seekcamera_error_get_str(event_status)
            message = ctypes.string_at(error).decode('utf-8', 'ignore') if error else str(event_status)
            failure = f'Seek camera error: {message}'
    if failure and coordinator:
        coordinator.camera_fault('seek', failure, event_status, generation, disconnected)


def _seek_event_loop():
    while not stop_all.is_set():
        try:
            event = seek_events.get(timeout=.1)
        except queue.Empty:
            continue
        try:
            _handle_seek_event(*event)
        except Exception as exc:
            if coordinator:
                coordinator.camera_fault('seek', str(exc), event[2], event[3])
            log.exception('Seek event processing failed')

c_frame_cb = FRAME_CALLBACK(on_frame_available)
c_event_cb = EVENT_CALLBACK(on_camera_event)

def start_seek(generation=None, cancel=None):
    _load_seek()
    global active_seek_cam, seek_session_generation, seek_event_worker
    if generation is None:
        generation = coordinator._health('seek')['generation'] if coordinator else 0
    if seek_event_worker is None or not seek_event_worker.is_alive():
        seek_event_worker = threading.Thread(target=_seek_event_loop, name='Seek device events', daemon=True)
        seek_event_worker.start()
    mgr = c_void_p()

    def wait_for(flag, seconds):
        deadline = time.monotonic() + seconds
        while not stop_all.is_set() and not (cancel and cancel.is_set()):
            if flag.wait(.05):
                return True
            if time.monotonic() >= deadline:
                return False
        return False
    try:
        with seek_control_lock:
            seek_session_generation = generation
            seek_connected.clear()
            first_seek_frame.clear()
            result = seek.seekcamera_manager_create(ctypes.byref(mgr), SEEK_IO_TYPE_USB)
            if result != 0 or not mgr:
                raise RuntimeError(f'Could not initialize the Seek camera manager (error {result}).')
            result = seek.seekcamera_manager_register_event_callback(mgr, c_event_cb, c_void_p(generation + 1))
            if result != 0:
                raise RuntimeError(f'Could not register the Seek camera callback (error {result}).')
        if not wait_for(seek_connected, 15) or not wait_for(first_seek_frame, 10):
            raise RuntimeError('Seek camera was not found or did not start streaming. Check its USB connection.')
        if (cancel and cancel.is_set()) or stop_all.is_set() or not seek_connected.is_set():
            raise RuntimeError('Seek connection cancelled or disconnected.')
        log.info('Seek streaming: generation=%s', generation)
        return mgr
    except Exception:
        try:
            _close_seek_manager(mgr)
        except Exception:
            # Retain ownership when an SDK rejects cleanup; do not leak or create
            # another manager over resources that the driver may still be using.
            if coordinator:
                with coordinator.lock:
                    coordinator.manager = mgr
            log.exception('Seek cleanup after failed connection')
        raise


def _close_seek_manager(manager):
    global active_seek_cam, seek_session_generation
    with seek_control_lock:
        seek_session_generation = -1
        seek_connected.clear()
        first_seek_frame.clear()
        if active_seek_cam:
            result = seek.seekcamera_capture_session_stop(active_seek_cam)
            if result != 0:
                log.warning('Seek capture stop returned %s during cleanup', result)
            active_seek_cam = None
        if manager:
            result = seek.seekcamera_manager_destroy(ctypes.byref(manager))
            if result != 0:
                raise RuntimeError(f'Seek manager cleanup failed (error {result}).')


def recover_seek(owner, generation, cancel):
    """Destroy/recreate only Seek resources; callbacks never perform this work."""
    _close_seek_manager(owner.manager)
    owner.manager = None
    if cancel.is_set():
        return
    manager = start_seek(generation, cancel)
    # Publishing a manager and cancellation are checked under the owner lock.
    with owner.lock:
        cancelled = cancel.is_set() or owner.closing
        if not cancelled:
            owner.manager = manager
    if cancelled:
        try:
            _close_seek_manager(manager)
        except Exception:
            with owner.lock:
                owner.manager = manager
            raise
        finally:
            owner.seek_ready = owner.cameras_ready = False

# ----------------- FLIR SETUP (FREE-RUNNING AT 30 FPS, MULTI-CAMERA) -----------------
def start_blackfly():
    global PySpin
    import PySpin
    system, cam_list, cams = start_flir_cameras(PySpin)
    try:
        from importlib.metadata import version
        log.info('Spinnaker/PySpin version: %s', version('spinnaker-python'))
    except Exception:
        log.info('Spinnaker/PySpin loaded: %s', getattr(PySpin, '__file__', 'unknown path'))
    for camera in cams:
        print(f"[FLIR] Camera {camera['index']} (serial {camera['serial']}) free-run acquisition started.")
        log.info('FLIR %s serial=%s started', camera['index'], camera['serial'])
    return system, cam_list, cams

# ----------------- WORKER THREADS -----------------
save_q = queue.Queue(maxsize=1024)

def flir_thread(cam_index, cam, generation=0, stop=None):
    timeouts = got = incomplete = 0
    timeout_reported = False
    while not stop_all.is_set() and not (stop and stop.is_set()):
        try:
            frame = read_flir_frame(cam)
        except PySpin.SpinnakerException as exc:
            timeouts += 1
            if timeouts >= 10 and not timeout_reported:
                if coordinator:
                    coordinator.camera_fault(f'flir_cam{cam_index}',
                        f'FLIR camera {cam_index} has stopped delivering frames: {exc}',
                        getattr(exc, 'errorcode', None), generation)
                timeout_reported = True
            stop_all.wait(.05)
            continue
        except Exception as exc:
            if coordinator:
                coordinator.camera_fault(f'flir_cam{cam_index}',
                    f'FLIR camera {cam_index} frame processing failed: {exc}', generation=generation)
            stop_all.wait(.05)
            continue

        if frame is None:
            incomplete += 1
            continue
        received_ns, frame_id, device_ns, data = frame
        got += 1
        if timeout_reported:
            ui_events.put(("message", f"FLIR camera {cam_index} is delivering frames again."))
            timeout_reported = False
        timeouts = 0
        wall_now = time.time()

        if coordinator is not None:
            coordinator.on_flir(cam_index, frame_id, device_ns, wall_now, data, received_ns,
                                generation=generation)

        try:
            if data.ndim == 2:
                preview = cv2.cvtColor(data, cv2.COLOR_BAYER_BG2BGR) if data.dtype == np.uint8 \
                          else cv2.cvtColor((data / 256).astype(np.uint8), cv2.COLOR_GRAY2BGR)
            else:
                preview = data
            with flir_preview_lock:
                flir_previews[cam_index] = preview
        except Exception as e:
            print(f"[FLIR cam {cam_index}] Preview error: {e}")

def trigger_seek_shutter():
    with seek_control_lock:
        cam = active_seek_cam
        if not cam or not seek_connected.is_set():
            raise RuntimeError('Seek is not connected for shutter capture.')
        return seek.seekcamera_shutter_trigger(cam)


def shutter_thread():
    next_t = None
    while not stop_all.is_set():
        if coordinator is not None and coordinator.shutter_tick():
            next_t = None
            stop_all.wait(.01)
            continue
        now = time.monotonic()
        with shutter_config_lock:
            timed = shutter_config["timed"]
            hz = shutter_config["hz"]
        if not timed or hz <= 0:
            next_t = None
            time.sleep(0.05)
            continue
        period = 1.0 / hz
        if next_t is None:
            next_t = now + period
        elif now >= next_t:
            if active_seek_cam and seek_connected.is_set():
                try:
                    res = trigger_seek_shutter()
                    if res != 0 and coordinator:
                        coordinator.camera_fault('seek', f'Seek shutter returned error {res}', res)
                except Exception as exc:
                    if coordinator:
                        coordinator.camera_fault('seek', str(exc))
            next_t = now + period
        time.sleep(0.05)



# Feature 4: Writer thread logs images, raw arrays, and complete header csv
# Now handles N FLIR cameras, each into its own subfolder and CSV.
def writer_thread(run_dir, cam_indices, ready_event=None):
    from ultramic_studio.recorder.storage import write_camera_files
    write_camera_files(sys.modules[__name__], run_dir, cam_indices, ready_event)
