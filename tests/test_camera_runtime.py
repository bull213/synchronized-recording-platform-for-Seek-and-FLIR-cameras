import weakref
from types import SimpleNamespace

import numpy as np
import pytest

import camera_runtime


@pytest.fixture
def source_folder(tmp_path, monkeypatch):
    monkeypatch.delenv('SYNC_SEEK_DLL', raising=False)
    monkeypatch.setitem(vars(camera_runtime), 'sys', SimpleNamespace(frozen=False))
    monkeypatch.setattr(camera_runtime, 'application_folder', lambda: tmp_path)
    monkeypatch.setattr(camera_runtime, 'resource_folder', lambda: tmp_path)
    return tmp_path


def test_source_launch_uses_latest_export_and_prefers_local_sdk(source_folder):
    for name in ('SyncRecorder_20261008_141014', 'SyncRecorder_20261008_141854'):
        exported = source_folder / 'exports' / name / 'SyncRecorder' / '_internal' / camera_runtime.SEEK_LIBRARY
        exported.parent.mkdir(parents=True)
        exported.write_bytes(b'sdk fixture')
    assert camera_runtime.find_seek_dll() == exported
    local = source_folder / camera_runtime.SEEK_LIBRARY
    local.parent.mkdir(parents=True)
    local.write_bytes(b'local sdk fixture')
    assert camera_runtime.find_seek_dll() == local


def test_seek_library_lookup_precedence(source_folder, monkeypatch):
    resources = source_folder / '_internal'
    monkeypatch.setattr(camera_runtime, 'resource_folder', lambda: resources)
    candidates = [resources / camera_runtime.SEEK_LIBRARY,
                  source_folder / camera_runtime.SEEK_LIBRARY,
                  source_folder / 'seekcamera.dll',
                  source_folder / 'exports' / '20261009' / 'SyncRecorder' / '_internal' / camera_runtime.SEEK_LIBRARY,
                  source_folder / 'exports' / '20261008' / 'SyncRecorder' / '_internal' / camera_runtime.SEEK_LIBRARY]
    override = source_folder / 'override.dll'
    for path in [override, *candidates]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'library fixture')
    monkeypatch.setenv('SYNC_SEEK_DLL', str(override))
    assert camera_runtime.find_seek_dll() == override
    override.unlink()
    # Even valid fallback libraries must not hide an incorrect override.
    with pytest.raises(FileNotFoundError, match='SYNC_SEEK_DLL'):
        camera_runtime.find_seek_dll()
    monkeypatch.delenv('SYNC_SEEK_DLL')
    for expected in candidates:
        assert camera_runtime.find_seek_dll() == expected
        expected.unlink()
    with pytest.raises(FileNotFoundError, match='Seek SDK library is missing'):
        camera_runtime.find_seek_dll()


def test_frozen_launch_excludes_source_transfer_bundles(source_folder, monkeypatch):
    exported = source_folder / 'exports' / '20261009' / 'SyncRecorder' / '_internal' / camera_runtime.SEEK_LIBRARY
    exported.parent.mkdir(parents=True)
    exported.write_bytes(b'exported library fixture')
    monkeypatch.setattr(camera_runtime.sys, 'frozen', True)
    with pytest.raises(FileNotFoundError, match='complete exported folder'):
        camera_runtime.find_seek_dll()


@pytest.mark.parametrize('failure', [None, 'incomplete', 'status', 'metadata', 'timestamp', 'pixels', 'copy'])
def test_image_buffer_always_released_and_pixels_copied(failure):
    data = np.zeros((3, 4), dtype=np.uint8)
    calls = []

    def incomplete():
        if failure == 'status':
            raise RuntimeError('bad image status')
        return failure == 'incomplete'

    def frame_id():
        if failure == 'metadata':
            raise RuntimeError('bad metadata')
        return 17

    def pixels():
        if failure == 'pixels':
            raise RuntimeError('bad pixels')
        if failure == 'copy':
            return SimpleNamespace(copy=fail_copy)
        return data

    def fail_copy():
        raise MemoryError('pixel copy failed')

    def timestamp():
        if failure == 'timestamp':
            raise RuntimeError('bad device timestamp')
        return 123

    image = SimpleNamespace(IsIncomplete=incomplete, GetFrameID=frame_id,
                            GetTimeStamp=timestamp, GetNDArray=pixels, Release=lambda: calls.append('release'))
    camera = SimpleNamespace(GetNextImage=lambda timeout: image)
    if failure in ('status', 'metadata', 'timestamp', 'pixels', 'copy'):
        with pytest.raises(MemoryError if failure == 'copy' else RuntimeError):
            camera_runtime.read_flir_frame(camera)
    else:
        frame = camera_runtime.read_flir_frame(camera)
        if failure == 'incomplete':
            assert frame is None
        else:
            received, fid, timestamp, copied = frame
            assert received > 0 and (fid, timestamp) == (17, 123)
            assert not np.shares_memory(copied, data)
    assert calls == ['release']


def test_frame_timeout_propagates_without_accessing_an_image():
    calls = []
    timeout_error = TimeoutError('frame timed out')

    def acquire(timeout):
        calls.append(timeout)
        raise timeout_error

    with pytest.raises(TimeoutError) as caught:
        camera_runtime.read_flir_frame(SimpleNamespace(GetNextImage=acquire))
    assert caught.value is timeout_error
    assert calls == [500]


def test_receipt_timestamp_precedes_processing_and_pixels_survive_release(monkeypatch):
    data = np.full((3, 4), 7, dtype=np.uint8)
    calls = []

    def acquire(timeout):
        assert timeout == 500
        calls.append('acquire')
        return image

    def clock():
        calls.append('timestamp')
        return 987654321

    def incomplete():
        assert calls == ['acquire', 'timestamp']
        return False

    def release():
        calls.append('release')
        data.fill(255)  # Simulate the SDK recycling its buffer immediately.

    image = SimpleNamespace(IsIncomplete=incomplete, GetFrameID=lambda: 17,
                            GetTimeStamp=lambda: 123, GetNDArray=lambda: data, Release=release)
    monkeypatch.setattr(camera_runtime.time, 'perf_counter_ns', clock)
    frame = camera_runtime.read_flir_frame(SimpleNamespace(GetNextImage=acquire))
    assert frame[:3] == (987654321, 17, 123)
    np.testing.assert_array_equal(frame[3], np.full((3, 4), 7, dtype=np.uint8))
    assert calls == ['acquire', 'timestamp', 'release']


@pytest.mark.parametrize('failure', ['enumeration', 'empty', 'init', 'serial', 'settings', 'begin', None])
def test_flir_initialization_cleans_up_partial_failures(failure):
    calls = []
    handles = []

    class Node:
        def SetValue(self, value):
            if failure == 'settings':
                raise RuntimeError('bad settings')

        def GetValue(self):
            if failure == 'serial':
                raise RuntimeError('bad serial')
            return 'camera serial'

        def GetEntryByName(self, name):
            return self

        def SetIntValue(self, value):
            pass

    class Camera:
        TLDevice = SimpleNamespace(DeviceSerialNumber=Node())

        def Init(self):
            calls.append('init')
            if failure == 'init':
                raise RuntimeError('bad init')

        def GetNodeMap(self):
            return SimpleNamespace(GetNode=lambda name: Node())

        GetTLStreamNodeMap = GetNodeMap

        def BeginAcquisition(self):
            calls.append('begin')
            if failure == 'begin':
                raise RuntimeError('bad acquisition')

        def EndAcquisition(self):
            calls.append('end')

        def DeInit(self):
            calls.append('deinit')

    class CameraList:
        def __init__(self):
            self.items = [] if failure == 'empty' else [Camera()]
            handles.extend(weakref.ref(cam) for cam in self.items)

        def GetSize(self):
            return len(self.items)

        def __getitem__(self, index):
            return self.items[index]

        def Clear(self):
            self.items.clear()
            calls.append('clear')

    def enumerate_cameras():
        if failure == 'enumeration':
            raise RuntimeError('bad enumeration')
        return CameraList()

    def release():
        assert all(handle() is None for handle in handles), 'SDK handle retained by failed setup'
        calls.append('release')

    system = SimpleNamespace(GetCameras=enumerate_cameras, ReleaseInstance=release)
    sdk = SimpleNamespace(System=SimpleNamespace(GetInstance=lambda: system),
                          CEnumerationPtr=lambda node: node, CBooleanPtr=lambda node: node,
                          CFloatPtr=lambda node: node, IsAvailable=lambda node: True, IsWritable=lambda node: True)
    if failure is None:
        owner, camera_list, records = camera_runtime.start_flir_cameras(sdk)
        assert records[0]['serial'] == 'camera serial' and records[0]['acquiring']
        assert not camera_runtime.release_flir_cameras(records, camera_list, owner)
        assert calls == ['init', 'begin', 'end', 'deinit', 'clear', 'release']
    else:
        with pytest.raises(RuntimeError, match='Could not initialize'):
            camera_runtime.start_flir_cameras(sdk)
        assert calls[-1] == 'release'
        if failure in ('serial', 'settings', 'begin'):
            assert 'deinit' in calls
        if failure == 'init':
            assert 'deinit' not in calls


@pytest.mark.parametrize('failure', ['init', 'enum', 'rate', 'begin'])
def test_second_camera_failure_releases_all_owned_handles(failure):
    calls, handles = [], []

    class Node:
        def __init__(self, camera, name):
            self.camera = camera  # SDK nodes may keep their owning camera alive.
            self.name = name

        def GetEntryByName(self, name):
            return SimpleNamespace(GetValue=lambda: 0)

        def SetIntValue(self, value):
            if self.camera.index == 1 and failure == 'enum':
                raise RuntimeError('second camera enum failed')

        def SetValue(self, value):
            if self.camera.index == 1 and self.name == 'AcquisitionFrameRate' and failure == 'rate':
                raise RuntimeError('second camera rate failed')

    class Camera:
        TLDevice = SimpleNamespace(DeviceSerialNumber=SimpleNamespace(GetValue=lambda: 'serial'))

        def __init__(self, index):
            self.index = index

        def Init(self):
            calls.append(f'init{self.index}')
            if self.index == 1 and failure == 'init':
                raise RuntimeError('second camera init failed')

        def GetNodeMap(self):
            return SimpleNamespace(GetNode=lambda name: Node(self, name))

        GetTLStreamNodeMap = GetNodeMap

        def BeginAcquisition(self):
            calls.append(f'begin{self.index}')
            if self.index == 1 and failure == 'begin':
                raise RuntimeError('second camera begin failed')

        def EndAcquisition(self):
            calls.append(f'end{self.index}')

        def DeInit(self):
            calls.append(f'deinit{self.index}')

    class CameraList:
        def __init__(self):
            self.items = [Camera(0), Camera(1)]
            handles.extend(weakref.ref(camera) for camera in self.items)

        def GetSize(self):
            return len(self.items)

        def __getitem__(self, index):
            return self.items[index]

        def Clear(self):
            self.items.clear()
            calls.append('clear')

    def release():
        assert all(handle() is None for handle in handles), 'Failed setup retained a camera or node'
        calls.append('release')

    system = SimpleNamespace(GetCameras=CameraList, ReleaseInstance=release)
    sdk = SimpleNamespace(System=SimpleNamespace(GetInstance=lambda: system),
                          CEnumerationPtr=lambda node: node, CBooleanPtr=lambda node: node,
                          CFloatPtr=lambda node: node, IsAvailable=lambda node: True, IsWritable=lambda node: True)
    with pytest.raises(RuntimeError) as caught:
        camera_runtime.start_flir_cameras(sdk)
    assert str(caught.value) == f'Could not initialize the FLIR cameras: second camera {failure} failed'
    expected = ['init0', 'begin0', 'init1']
    if failure == 'begin':
        expected.append('begin1')
    expected.extend(['end0', 'deinit0'])
    if failure != 'init':
        expected.append('deinit1')
    assert calls == [*expected, 'clear', 'release']


def test_multiple_cleanup_errors_do_not_retain_handles_or_skip_later_resources():
    calls = []

    class Camera:
        def __init__(self, index):
            self.index = index

        def EndAcquisition(self):
            calls.append(f'end{self.index}')
            raise RuntimeError(f'end{self.index}')

        def DeInit(self):
            calls.append(f'deinit{self.index}')
            raise RuntimeError(f'deinit{self.index}')

    records = [dict(cam=Camera(0), acquiring=True),
               dict(cam=Camera(1), acquiring=True, initialized=True), {}, dict(cam=None)]
    shared_records = records.copy()
    handles = [weakref.ref(record['cam']) for record in records if record.get('cam') is not None]

    def clear():
        assert records == []
        assert all('cam' not in record for record in shared_records)
        assert all(handle() is None for handle in handles)
        calls.append('clear')
        raise RuntimeError('clear')

    def release():
        calls.append('release')
        raise RuntimeError('release')

    errors = camera_runtime.release_flir_cameras(records, SimpleNamespace(Clear=clear),
                                                 SimpleNamespace(ReleaseInstance=release))
    assert calls == ['end0', 'deinit0', 'end1', 'deinit1', 'clear', 'release']
    assert errors == ['FLIR acquisition cleanup error: end0', 'FLIR camera cleanup error: deinit0',
                      'FLIR acquisition cleanup error: end1', 'FLIR camera cleanup error: deinit1',
                      'FLIR camera list cleanup error: clear', 'FLIR system cleanup error: release']


@pytest.mark.parametrize('optional_node', ['AcquisitionFrameRateEnable', 'AcquisitionFrameRate'])
@pytest.mark.parametrize('available,writable', [(False, True), (True, False), (True, True)])
def test_optional_frame_rate_nodes_and_required_camera_settings(optional_node, available, writable):
    writes, lifecycle = [], []

    class Node:
        def __init__(self, name):
            self.name = name

        def GetEntryByName(self, name):
            return SimpleNamespace(GetValue=lambda: name)

        def SetValue(self, value):
            writes.append((self.name, value))

        SetIntValue = SetValue

    def is_writable(node):
        if node.name == optional_node:
            assert available, 'Must not query writability of an unavailable node'
            return writable
        return True

    camera = SimpleNamespace(
        Init=lambda: lifecycle.append('init'), BeginAcquisition=lambda: lifecycle.append('begin'),
        TLDevice=SimpleNamespace(DeviceSerialNumber=SimpleNamespace(GetValue=lambda: 'serial')),
        GetNodeMap=lambda: SimpleNamespace(GetNode=Node),
        GetTLStreamNodeMap=lambda: SimpleNamespace(GetNode=Node))
    sdk = SimpleNamespace(CEnumerationPtr=lambda node: node, CBooleanPtr=lambda node: node,
                          CFloatPtr=lambda node: node, IsWritable=is_writable,
                          IsAvailable=lambda node: node.name != optional_node or available)
    record = dict(cam=camera, initialized=False, acquiring=False)
    camera_runtime._configure_flir_camera(sdk, record)
    expected = [('TriggerMode', 'Off'), ('AcquisitionFrameRateEnable', True),
                ('AcquisitionFrameRate', 30.0), ('ExposureAuto', 'Off'),
                ('ExposureTime', 10000.0), ('StreamBufferHandlingMode', 'NewestOnly')]
    if not (available and writable):
        expected = [setting for setting in expected if setting[0] != optional_node]
    assert writes == expected
    assert lifecycle == ['init', 'begin']
    assert record['initialized'] is True and record['acquiring'] is True and record['serial'] == 'serial'
