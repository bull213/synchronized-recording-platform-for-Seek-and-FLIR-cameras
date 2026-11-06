"""Characterize the native Seek ABI without loading a hardware SDK."""
import ctypes

from ultramic_studio.recorder.cameras import capture, seek_sdk


def test_seek_header_layout_and_callback_identity_are_preserved():
    header = capture.SeekFrameHeader
    assert header is seek_sdk.SeekFrameHeader
    assert ctypes.sizeof(header) == 346
    assert len(header._fields_) == 39
    assert {name: getattr(header, name).offset for name in (
        'sentinel', 'width', 'timestamp_utc_ns', 'chipid', 'fpa_frame_count',
        'histeq_agc_reserved', 'linear_agc_reserved', 'flat_scene_correction_filter_state',
    )} == {
        'sentinel': 0, 'width': 9, 'timestamp_utc_ns': 22, 'chipid': 30,
        'fpa_frame_count': 99, 'histeq_agc_reserved': 144,
        'linear_agc_reserved': 216, 'flat_scene_correction_filter_state': 345,
    }
    assert capture.FRAME_CALLBACK is seek_sdk.FRAME_CALLBACK
    assert capture.EVENT_CALLBACK is seek_sdk.EVENT_CALLBACK
    assert isinstance(capture.c_frame_cb, capture.FRAME_CALLBACK)
    assert isinstance(capture.c_event_cb, capture.EVENT_CALLBACK)


def test_lazy_loader_applies_every_signature_once(tmp_path, monkeypatch):
    class Function:
        pass

    class Library:
        def __init__(self):
            self.functions = {}

        def __getattr__(self, name):
            return self.functions.setdefault(name, Function())

    library = Library()
    calls = []
    monkeypatch.setattr(capture, 'seek', None)
    monkeypatch.setattr(capture, 'find_seek_dll', lambda: tmp_path / 'seekcamera.dll')
    monkeypatch.setattr(capture.os, 'add_dll_directory', lambda path: object(), raising=False)
    monkeypatch.setattr(capture.ctypes, 'CDLL', lambda path: calls.append(path) or library)
    capture._load_seek()
    capture._load_seek()
    assert len(calls) == 1
    assert capture.seek is library
    assert capture._seek_signatures is seek_sdk._seek_signatures
    assert len(library.functions) == len(seek_sdk._seek_signatures) == 30
    for name, argtypes, restype in seek_sdk._seek_signatures:
        assert library.functions[name].argtypes == argtypes
        assert library.functions[name].restype == restype
