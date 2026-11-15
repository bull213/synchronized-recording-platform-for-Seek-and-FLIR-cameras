"""Tests for independent Seek-only (thermal only) and FLIR-only Cage ROI operations."""
import csv
import json
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

from test_laptop_layout import window, pump
from test_thermal_roi import recording
import thermal_roi as roi
import thermal_roi_gui


def flir_recording(root, count=6, shape=(6, 8)):
    """Create a synthetic FLIR-only recording without any Seek files."""
    root.mkdir(parents=True, exist_ok=True)
    folder = root / 'video_parts'
    folder.mkdir(exist_ok=True)
    h, w = shape
    video_name = 'video_parts/flir_cam0_take01_part0001.mp4'
    video_path = root / video_name
    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*'mp4v'), 4, (w, h))
    assert writer.isOpened()
    for index in range(count):
        img = np.zeros((h, w, 3), np.uint8)
        img[:, :w // 2] = (10, 200, 10)
        img[:, w // 2:] = (200, 10, 10)
        writer.write(img)
    writer.release()

    log_path = root / 'flir_log_cam0.csv'
    with log_path.open('w', newline='', encoding='utf-8') as handle:
        w_csv = csv.writer(handle)
        w_csv.writerow(['idx', 'frame_id', 'cam_ts_ns', 'pc_time', 'take_id',
                        'host_received_monotonic_ns', 'relative_to_seek_ns',
                        'timestamp_source', 'connection_generation', 'video_file', 'part_frame_index'])
        for i in range(count):
            w_csv.writerow([i, 100 + i, 1_790_000_000_000_000_000 + i * 250_000_000,
                            f'{1790000000.0 + i * 0.25:.6f}', 1,
                            500_000_000_000 + i * 250_000_000, i * 250_000_000,
                            'host_frame_receipt', 0, video_name, i])
    return root


def cages():
    return [{'mouse_id': 'WT_01', 'genotype': 'WT', 'roi': [0, 0, 3, 5]},
            {'mouse_id': 'WT_02', 'genotype': 'WT', 'roi': [4, 0, 4, 6]}]


def test_seek_only_without_video_parts_exports_cleanly(tmp_path):
    """A recording with Seek thermal CSV + log but no video parts must export without error."""
    root = tmp_path / 'seek_only_no_video'
    recording(root, count=10)
    # Ensure no video files or video_parts directory exist
    assert not (root / 'video_parts').exists()

    result = thermal_roi_gui.inspect_recording(root)
    assert result.has_seek is True
    assert result.has_video is False
    assert result.active_stream == 'seek'

    # Export rois with include_video=False (default for thermal only)
    config = roi.make_config(root, (6, 8), cages())
    out = roi.export_rois(root, config, tmp_path / 'output', include_video=False)
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'complete'
    assert manifest['frames'] == 10
    assert 'video_sources' not in manifest

    for cage in cages():
        mouse_folder = out / cage['mouse_id']
        assert (mouse_folder / 'temperature_timeseries.csv').is_file()
        assert len(list((mouse_folder / 'pixels').glob('*.npz'))) == 1
        # No video parts folder should be created
        assert not (mouse_folder / 'video_parts').exists()


def test_seek_only_gui_disables_video_and_navigates_thermal(window, tmp_path):
    """In GUI, missing Seek video parts disable the video checkbox and slider uses thermal decoding."""
    app, recorder = window
    root = tmp_path / 'seek_no_video_gui'
    recording(root, count=5)

    recorder.controller.run_dir = root
    recorder.open_cage_roi()
    tool = recorder._roi_window
    pump(app, lambda: tool.thread is None and tool.frame is not None)

    assert tool.active_modality == 'thermal'
    assert tool.has_video is False
    assert not tool.include_video.isChecked()
    assert not tool.include_video.isEnabled()
    assert tool.native_button.isEnabled()

    # Move slider and browse
    tool.slider.setValue(2)
    tool.browse_video()
    pump(app, lambda: tool.thread is None)
    assert 'Native thermal frame 2' in tool.preview_label.text()
    tool.close()


def test_flir_only_recording_inspect_and_export(tmp_path):
    """FLIR-only recording is inspected and cropped into per-cage MP4s without Seek files."""
    root = tmp_path / 'flir_only_rec'
    flir_recording(root, count=8, shape=(6, 8))

    # Seek files must not exist
    assert not (root / 'seek_thermal.csv').exists()
    assert not (root / 'seek_log.csv').exists()

    result = thermal_roi_gui.inspect_recording(root)
    assert result.has_seek is False
    assert result.has_flir is True
    assert result.active_stream == 'flir_cam0'
    stream_info = result.streams['flir_cam0']
    assert stream_info.shape == (6, 8)
    assert stream_info.has_video is True
    assert len(stream_info.previews) == 8

    config = roi.make_config(root, (6, 8), cages(), stream_info=stream_info)
    out = roi.export_rois(root, config, tmp_path / 'output', stream_info=stream_info)

    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'complete'
    assert manifest['modality'] == 'flir_video'
    assert manifest['frames'] == 8

    for cage in cages():
        folder = out / cage['mouse_id']
        assert (folder / 'video_frame_map.csv').is_file()
        parts = list((folder / 'video_parts').glob('*.mp4'))
        assert len(parts) == 1
        cap = cv2.VideoCapture(str(parts[0]))
        try:
            assert cap.isOpened()
            count = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                count += 1
            assert count == 8
        finally:
            cap.release()


def test_flir_only_gui_loads_and_exports(window, tmp_path, monkeypatch):
    """Recorder opens a finished FLIR-only take in Cage ROI, defines cages, and exports."""
    app, recorder = window
    root = tmp_path / 'flir_gui_rec'
    flir_recording(root, count=6, shape=(6, 8))

    recorder.controller.run_dir = root
    recorder.open_cage_roi()
    tool = recorder._roi_window
    pump(app, lambda: tool.thread is None and tool.frame is not None)

    assert tool.active_modality == 'video'
    assert tool.active_stream_key == 'flir_cam0'
    assert not tool.native_button.isEnabled()
    assert tool.include_video.isChecked()
    assert tool.export_button.text() == 'Export cropped FLIR video…'

    tool.change_roi(-1, [0, 0, 3, 5])
    tool.review.setChecked(True)
    monkeypatch.setattr(thermal_roi_gui.W.QFileDialog, 'getExistingDirectory', lambda *args: str(tmp_path / 'out'))

    tool.export()
    pump(app, lambda: tool.thread is None)
    assert tool.last_export is not None
    assert (Path(tool.last_export) / 'manifest.json').is_file()
    manifest = json.loads((Path(tool.last_export) / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'complete'
    assert manifest['modality'] == 'flir_video'
    tool.close()


def test_combined_recording_stream_selector(window, tmp_path):
    """In a recording with both Seek and FLIR, user can switch between streams."""
    app, recorder = window
    root = tmp_path / 'combined_rec'
    recording(root, count=4)
    flir_recording(root, count=4, shape=(10, 12))

    recorder.controller.run_dir = root
    recorder.open_cage_roi()
    tool = recorder._roi_window
    pump(app, lambda: tool.thread is None and tool.frame is not None)

    assert tool.stream_selector.isVisible()
    assert tool.stream_selector.count() >= 2
    # Initially Seek
    assert tool.active_stream_key == 'seek'
    assert tool.active_modality == 'thermal'
    assert tool.coordinate_fields[2].maximum() == 8

    # Switch to FLIR
    flir_idx = tool.stream_selector.findData('flir_cam0')
    assert flir_idx >= 0
    tool.stream_selector.setCurrentIndex(flir_idx)
    assert tool.active_stream_key == 'flir_cam0'
    assert tool.active_modality == 'video'
    assert tool.coordinate_fields[2].maximum() == 12  # FLIR width
    tool.close()


def test_seek_gui_missing_video_parts_forces_include_video_false(window, tmp_path, monkeypatch):
    """When video files are missing, GUI disables include_video and exports thermal cleanly."""
    app, recorder = window
    root = tmp_path / 'seek_missing_video'
    recording(root, count=6)
    assert not (root / 'video_parts').exists()

    recorder.controller.run_dir = root
    recorder.open_cage_roi()
    tool = recorder._roi_window
    pump(app, lambda: tool.thread is None and tool.frame is not None)

    assert not tool.has_video
    assert not tool.include_video.isChecked()
    assert not tool.include_video.isEnabled()

    # Even if somehow checked, GUI export forces it to False
    tool.include_video.setChecked(True)
    tool.change_roi(-1, [0, 0, 3, 5])
    tool.review.setChecked(True)
    monkeypatch.setattr(thermal_roi_gui.W.QFileDialog, 'getExistingDirectory', lambda *args: str(tmp_path / 'out'))

    tool.export()
    pump(app, lambda: tool.thread is None)
    assert tool.last_export is not None
    manifest = json.loads((Path(tool.last_export) / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'complete'
    tool.close()

