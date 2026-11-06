"""Locate camera libraries, select Seek shutter frames, and manage FLIR resources."""
from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from ..paths import application_folder, resource_folder

if TYPE_CHECKING:
    from numpy.typing import NDArray


SEEK_LIBRARY = Path('camera_sdk') / 'seek' / 'seekcamera.dll'
_FLIR_FRAME_RATE_HZ = 30.0
_FLIR_EXPOSURE_US = 10000.0
_FLIR_FRAME_TIMEOUT_MS = 500


class SeekShutterFrameGate:
    """Select one fresh callback after a shutter request, not shutter completion.

    SDK calls run outside the lock so synchronous callbacks cannot deadlock.
    Cancellation invalidates an in-flight call before it can publish its result.
    """

    def __init__(self, clock=time.perf_counter_ns):
        self.clock = clock
        self.lock = threading.Lock()
        self.sequence = 0
        self.pending = None

    def begin(self, take_id):
        with self.lock:
            if self.pending is not None:
                return None
            self.sequence += 1
            self.pending = dict(shutter_request_id=self.sequence, take_id=take_id,
                                shutter_request_monotonic_ns=self.clock())
            return self.sequence

    def request(self, token, trigger):
        """Call the injected shutter function; publish only successful requests."""
        with self.lock:
            if self.pending is None or self.pending['shutter_request_id'] != token:
                return None
        try:
            result = trigger()
            returned_ns = self.clock()
            if result != 0:
                raise RuntimeError(f'Seek shutter request failed (error {result}).')
        except Exception:
            with self.lock:
                if self.pending and self.pending['shutter_request_id'] == token:
                    self.pending = None
            raise
        with self.lock:
            if self.pending is None or self.pending['shutter_request_id'] != token:
                return None
            self.pending['shutter_return_monotonic_ns'] = returned_ns
            return dict(self.pending)

    def consume(self, received_ns, take_id, valid=True):
        with self.lock:
            pending = self.pending
            if (not valid or pending is None or pending['take_id'] != take_id or
                    'shutter_return_monotonic_ns' not in pending or
                    received_ns <= pending['shutter_return_monotonic_ns']):
                return None
            self.pending = None
            return {**pending, 'shutter_frame_received_monotonic_ns': received_ns,
                    'shutter_timing': 'after_successful_request'}

    def expired(self, timeout_ns=5_000_000_000):
        with self.lock:
            return (self.pending is not None and
                    self.clock() - self.pending['shutter_request_monotonic_ns'] >= timeout_ns)

    def cancel(self):
        with self.lock:
            self.pending = None


class _FlirCameraRecord(TypedDict, total=False):
    """Shared metadata whose SDK handle is removed when ownership ends.

    Fields are optional to accommodate partially initialized and legacy records.
    SDK objects remain duck typed so this module needs no PySpin import.
    """

    index: int
    cam: Any
    initialized: bool
    acquiring: bool
    serial: str


def _seek_candidates() -> Iterator[Path]:
    """Search application-relative libraries and local transfer bundles."""
    app = application_folder()
    yield resource_folder() / SEEK_LIBRARY
    yield app / SEEK_LIBRARY
    yield app / 'seekcamera.dll'
    if not getattr(sys, 'frozen', False):
        # Use the same SDK when launching from the IDE, CMD or desktop shortcut.
        yield from sorted((app / 'exports').glob(f'*/SyncRecorder/_internal/{SEEK_LIBRARY.as_posix()}'),
                          reverse=True)


def find_seek_dll() -> Path:
    """Resolve the SDK library; an invalid explicit override must fail."""
    override = os.environ.get('SYNC_SEEK_DLL')
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f'SYNC_SEEK_DLL does not point to a file: {path}')
        return path
    for path in _seek_candidates():
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError('Seek SDK library is missing. Use the complete exported folder, '
                            'or set SYNC_SEEK_DLL to your installed seekcamera.dll.')


def _release_flir_camera(record: _FlirCameraRecord) -> list[str]:
    """Detach a shared handle and finish camera cleanup before returning."""
    camera = record.pop('cam', None)
    if camera is None:
        return []
    errors: list[str] = []
    try:
        if record.get('acquiring'):
            camera.EndAcquisition()
    except Exception as exc:
        errors.append(f'FLIR acquisition cleanup error: {exc}')
    try:
        # Older callers do not supply an initialization flag.
        if record.get('initialized', True):
            camera.DeInit()
    except Exception as exc:
        errors.append(f'FLIR camera cleanup error: {exc}')
    finally:
        del camera
    return errors


def release_flir_cameras(cameras: list[_FlirCameraRecord], camera_list: Any,
                         system: Any) -> list[str]:
    """Release every SDK resource, returning errors without retaining tracebacks.

    Acquisition threads must have stopped before calling this. Shared camera
    records lose their handles so queued UI events cannot retain SDK references.
    """
    errors: list[str] = []
    for record in cameras:
        errors.extend(_release_flir_camera(record))
    cameras.clear()
    if camera_list is not None:
        try:
            camera_list.Clear()
        except Exception as exc:
            errors.append(f'FLIR camera list cleanup error: {exc}')
    if system is not None:
        try:
            system.ReleaseInstance()
        except Exception as exc:
            errors.append(f'FLIR system cleanup error: {exc}')
    return errors


def _set_enum(sdk: Any, node_map: Any, name: str, value: str) -> None:
    """Set a required enumeration node, propagating unsupported settings."""
    node = sdk.CEnumerationPtr(node_map.GetNode(name))
    node.SetIntValue(node.GetEntryByName(value).GetValue())


def _set_if_writable(sdk: Any, node: Any, value: bool | float) -> None:
    """Set an optional node only when the SDK reports it usable."""
    if sdk.IsAvailable(node) and sdk.IsWritable(node):
        node.SetValue(value)


def _configure_flir_camera(sdk: Any, record: _FlirCameraRecord) -> None:
    """Initialize an owned camera, recording each completed lifecycle step."""
    camera = record['cam']
    camera.Init()
    record['initialized'] = True
    record['serial'] = camera.TLDevice.DeviceSerialNumber.GetValue()
    nodes = camera.GetNodeMap()

    _set_enum(sdk, nodes, 'TriggerMode', 'Off')
    enable_rate = sdk.CBooleanPtr(nodes.GetNode('AcquisitionFrameRateEnable'))
    _set_if_writable(sdk, enable_rate, True)
    rate = sdk.CFloatPtr(nodes.GetNode('AcquisitionFrameRate'))
    _set_if_writable(sdk, rate, _FLIR_FRAME_RATE_HZ)
    _set_enum(sdk, nodes, 'ExposureAuto', 'Off')
    sdk.CFloatPtr(nodes.GetNode('ExposureTime')).SetValue(_FLIR_EXPOSURE_US)
    _set_enum(sdk, camera.GetTLStreamNodeMap(), 'StreamBufferHandlingMode', 'NewestOnly')
    camera.BeginAcquisition()
    record['acquiring'] = True


def start_flir_cameras(sdk: Any) -> tuple[Any, Any, list[_FlirCameraRecord]]:
    """Open all FLIRs, cleaning up even when enumeration or setup fails."""
    system = sdk.System.GetInstance()
    camera_list = None
    records: list[_FlirCameraRecord] = []
    try:
        camera_list = system.GetCameras()
        count = camera_list.GetSize()
        if not count:
            raise RuntimeError('No FLIR camera was found. Check its connection and power.')
        for index in range(count):
            # Register ownership before Init or reading serial metadata can fail.
            record: _FlirCameraRecord = dict(index=index, cam=camera_list[index],
                                             initialized=False, acquiring=False)
            records.append(record)
            _configure_flir_camera(sdk, record)
    except Exception as exc:
        # SDK node/camera handles can be held by failed configuration frames.
        # Drop those frames before releasing the SDK and preserve the error text.
        message = str(exc)
        exc.__traceback__ = None
        errors = release_flir_cameras(records, camera_list, system)
        details = '; '.join([message, *errors])
        raise RuntimeError(f'Could not initialize the FLIR cameras: {details}') from None
    return system, camera_list, records


def read_flir_frame(camera: Any) -> tuple[int, int, int, NDArray[Any]] | None:
    """Return receipt time, frame ID, device time and owned pixels, or None.

    Receipt time is sampled immediately after acquisition. Once an image is
    obtained, its SDK buffer is released after checking and copying its pixels.
    """
    image = camera.GetNextImage(_FLIR_FRAME_TIMEOUT_MS)
    received_ns = time.perf_counter_ns()
    try:
        if image.IsIncomplete():
            return None
        return received_ns, image.GetFrameID(), image.GetTimeStamp(), image.GetNDArray().copy()
    finally:
        image.Release()


def reconnect_flir_camera(sdk: Any, system: Any, record: _FlirCameraRecord) -> None:
    """Replace one stopped camera by serial, without releasing the shared system.

    The caller must join this camera's acquisition worker before calling, and
    serialize enumeration with other recovery attempts. Other cameras stay live.
    """
    serial = record['serial']
    errors = _release_flir_camera(record)
    record.update(initialized=False, acquiring=False)
    camera_list = None
    candidate = None
    try:
        camera_list = system.GetCameras()
        for index in range(camera_list.GetSize()):
            candidate = camera_list[index]
            if candidate.TLDevice.DeviceSerialNumber.GetValue() == serial:
                record['cam'] = candidate
                candidate = None
                break
            candidate = None
        if 'cam' not in record:
            raise RuntimeError(f'FLIR serial {serial} is not connected.')
        _configure_flir_camera(sdk, record)
    except Exception as exc:
        message = str(exc)
        exc.__traceback__ = None
        candidate = None
        errors.extend(_release_flir_camera(record))
        raise RuntimeError('; '.join([message, *errors])) from None
    finally:
        candidate = None
        if camera_list is not None:
            camera_list.Clear()
