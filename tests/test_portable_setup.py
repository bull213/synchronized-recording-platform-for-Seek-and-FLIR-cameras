import os
import sys

import pytest

import camera_runtime
from recorder_setup import self_test


def test_frozen_seek_sdk_is_found_in_moved_bundle(tmp_path, monkeypatch):
    app = tmp_path / 'Different drive and folder' / 'SyncRecorder'
    resources = app / '_internal'
    dll = resources / 'camera_sdk' / 'seek' / 'seekcamera.dll'
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b'library locator fixture')
    monkeypatch.delenv('SYNC_SEEK_DLL', raising=False)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(resources), raising=False)
    monkeypatch.setattr(sys, 'executable', str(app / 'SyncRecorder.exe'))
    assert camera_runtime.application_folder() == app
    assert camera_runtime.find_seek_dll() == dll
    dll.unlink()
    with pytest.raises(FileNotFoundError, match='complete exported folder'):
        camera_runtime.find_seek_dll()


def test_seek_override_is_explicit_and_does_not_fall_back_when_wrong(tmp_path, monkeypatch):
    dll = tmp_path / 'seekcamera.dll'
    monkeypatch.setenv('SYNC_SEEK_DLL', str(dll))
    with pytest.raises(FileNotFoundError, match='SYNC_SEEK_DLL'):
        camera_runtime.find_seek_dll()
    dll.write_bytes(b'library locator fixture')
    assert camera_runtime.find_seek_dll() == dll


def test_portable_recording_self_test_writes_a_success_report(tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    report = tmp_path / 'self_test_report.txt'
    assert self_test(report) == 0
    content = report.read_text(encoding='utf-8')
    assert 'SELF-TEST: PASSED' in content
    assert 'two FLIR MP4s' in content
    assert 'Cage selector and per-cage thermal/video export' in content
    assert 'hardware was not opened' in content
