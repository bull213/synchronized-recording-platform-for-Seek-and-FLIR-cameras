import csv
import hashlib
import json
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

import thermal_roi as roi
from test_thermal_roi import recording
from test_laptop_layout import window, pump


def video_recording(root):
    recording(root, count=6)
    log = root / 'seek_log.csv'
    with log.open(newline='') as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    folder = root / 'video_parts'
    folder.mkdir()
    for part in range(2):
        name = f'seek_take{part + 1:02d}_part0001.mp4'
        video = cv2.VideoWriter(str(folder / name), cv2.VideoWriter_fourcc(*'mp4v'), 4, (8, 6))
        assert video.isOpened()
        for index in range(3):
            image = np.zeros((6, 8, 3), np.uint8)
            image[:, :4] = (10, 10, 240)
            image[:, 4:] = (240, 10, 10)
            video.write(image)
            rows[part * 3 + index]['file'] = 'video_parts/' + name
            rows[part * 3 + index]['part_frame_index'] = str(index)
        video.release()
    with log.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def selections():
    return [{'mouse_id': 'left', 'genotype': 'WT', 'roi': [0, 0, 3, 5]},
            {'mouse_id': 'right', 'genotype': 'WT', 'roi': [4, 0, 4, 6]}]


def test_video_and_thermal_exports_follow_parts_and_preserve_source(tmp_path):
    root = tmp_path / 'recording'
    rows = video_recording(root)
    before = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob('*') if p.is_file()}
    out = roi.export_rois(root, roi.make_config(root, (6, 8), selections()), tmp_path / 'output', include_video=True)
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['status'] == 'complete'
    assert [part['frames'] for part in manifest['video_parts']] == [3, 3]
    for cage in selections():
        folder = out / cage['mouse_id']
        with (folder / 'video_frame_map.csv').open(newline='') as handle:
            mapping = list(csv.DictReader(handle))
        assert [r['source_frame_index'] for r in mapping] == [r['idx'] for r in rows]
        assert [r['timestamp_utc_ns'] for r in mapping] == [r['sdk_utc_ns'] for r in rows]
        assert [int(r['output_part_frame_index']) for r in mapping] == [0, 1, 2, 0, 1, 2]
        for name in sorted((folder / 'video_parts').glob('*.mp4')):
            cap = cv2.VideoCapture(str(name))
            try:
                assert cap.get(cv2.CAP_PROP_FPS) == pytest.approx(4)
                frames = []
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    frames.append(frame)
                assert len(frames) == 3
                assert frames[0].shape[:2] == (6, 4)
                mean = frames[0].mean(axis=(0, 1))
                assert (mean[2] > mean[0] + 100) if cage['mouse_id'] == 'left' else (mean[0] > mean[2] + 100)
            finally:
                cap.release()
        with np.load(next((folder / 'pixels').glob('*.npz'))) as arrays:
            assert arrays['pixels'].shape[1:] == (cage['roi'][3], cage['roi'][2])
    assert before == {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('failure', ['missing', 'index', 'cancel', 'changed'])
def test_video_failure_leaves_incomplete_manifest(tmp_path, failure):
    root = tmp_path / 'recording'
    video_recording(root)
    log = root / 'seek_log.csv'
    if failure == 'missing':
        next((root / 'video_parts').glob('*.mp4')).unlink()
    elif failure == 'index':
        text = log.read_text()
        log.write_text(text.replace('part0001.mp4,1', 'part0001.mp4,9', 1))
    cancel = threading.Event()
    changed = False
    def progress(count, fraction, eta):
        nonlocal changed
        if failure == 'cancel' and fraction > .5:
            cancel.set()
        if failure == 'changed' and not changed and fraction > 0:
            path = next((root / 'video_parts').glob('*.mp4'))
            with path.open('ab') as handle:
                handle.write(b'changed')
            changed = True
    config = roi.make_config(root, (6, 8), selections())
    with pytest.raises((ValueError, OSError, roi.ExportCancelled)):
        roi.export_rois(root, config, tmp_path / 'out', cancel, progress, include_video=True)
    manifest = json.loads(next((tmp_path / 'out').glob('*/manifest.json')).read_text())
    assert manifest['status'] == ('cancelled' if failure == 'cancel' else 'incomplete')


def test_sample_video_can_stop_before_end_of_a_part(tmp_path):
    root = tmp_path / 'recording'
    video_recording(root)
    out = roi.export_rois(root, roi.make_config(root, (6, 8), selections()), tmp_path / 'out',
                          max_frames=2, include_video=True)
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['status'] == 'complete' and manifest['scope'] == 'sample'
    assert manifest['video_parts'][0]['frames'] == 2


def test_coordinator_guard_blocks_arm_and_manual_start_and_releases():
    from ultramic_studio.recorder.cameras.demo import DemoCameraCore
    from ultramic_studio.recorder.coordinator import RecordingCoordinator
    controller = RecordingCoordinator(DemoCameraCore())
    controller.begin_roi_export()
    assert not controller.ready()
    assert 'cage export' in controller.request('START')['error']
    assert 'cage export' in controller.request('ARM')['error']
    assert not controller.request('START', source='tcp')['ok']
    with pytest.raises(RuntimeError):
        controller.begin_roi_export()
    controller.end_roi_export()
    controller.armed = True
    with pytest.raises(RuntimeError, match='disarm'):
        controller.begin_roi_export()
    controller.armed = False
    controller.state = 'finalizing'
    with pytest.raises(RuntimeError, match='finish'):
        controller.begin_roi_export()


def test_recorder_opens_secondary_tool_and_guards_export(window, tmp_path, monkeypatch):
    import thermal_roi_gui

    app, recorder = window
    root = tmp_path / 'finished'
    video_recording(root)
    recorder.controller.run_dir = root
    recorder.open_cage_roi()
    tool = recorder._roi_window
    assert tool is not None and tool.isVisible() and tool.include_video.isChecked()
    pump(app, lambda: tool.thread is None and tool.frame is not None)
    assert Path(tool.root) == root
    tool.change_roi(-1, [0, 0, 3, 5])
    tool.review.setChecked(True)
    monkeypatch.setattr(thermal_roi_gui.W.QFileDialog, 'getExistingDirectory', lambda *args: str(tmp_path))
    started, release = threading.Event(), threading.Event()

    def blocking_export(*args, **kwargs):
        assert kwargs['include_video'] is True
        started.set()
        assert release.wait(10)
        return tmp_path

    monkeypatch.setattr(thermal_roi_gui, 'export_rois', blocking_export)
    tool.export()
    try:
        pump(app, started.is_set)
        assert recorder.controller.roi_export_active
        assert not recorder.record.isEnabled()
        assert not recorder.controller.request('START')['ok']
    finally:
        release.set()
    pump(app, lambda: tool.thread is None)
    assert not recorder.controller.roi_export_active
    tool.close()
    recorder.open_cage_roi()
    assert recorder._roi_window is tool and tool.isVisible()


def test_recorder_close_cancels_and_waits_for_cage_worker(window, tmp_path, monkeypatch):
    import thermal_roi_gui

    app, recorder = window
    root = tmp_path / 'finished'
    video_recording(root)
    recorder.controller.run_dir = root
    recorder.open_cage_roi()
    tool = recorder._roi_window
    pump(app, lambda: tool.thread is None and tool.frame is not None)
    tool.change_roi(-1, [0, 0, 3, 5])
    tool.review.setChecked(True)
    monkeypatch.setattr(thermal_roi_gui.W.QFileDialog, 'getExistingDirectory', lambda *args: str(tmp_path))
    started = threading.Event()

    def cancellable_export(*args, **kwargs):
        started.set()
        assert tool.cancel_event.wait(10)
        raise roi.ExportCancelled('Cancelled for shutdown')

    monkeypatch.setattr(thermal_roi_gui, 'export_rois', cancellable_export)
    tool.export()
    pump(app, started.is_set)
    recorder.close()
    assert tool.cancel_event.is_set()
    pump(app, lambda: tool.thread is None and recorder._shutdown_complete, seconds=25)
    assert not recorder.controller.roi_export_active
