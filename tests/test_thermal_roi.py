import csv
import hashlib
import json
import os
from pathlib import Path
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
import pandas as pd
import pytest

import thermal_roi as roi


def recording(root, count=5, nonfinite=False):
    root.mkdir()
    arrays = []
    with (root / 'seek_thermal.csv').open('w', newline='') as raw, (root / 'seek_log.csv').open('w', newline='') as log:
        writer = csv.writer(log)
        writer.writerow(['idx', 'take_id', 'sdk_utc_ns', 'host_received_monotonic_ns', 'calibration', 'file', 'part_frame_index'])
        for i in range(count):
            take, utc, host = 1 + i // 3, 1_790_000_000_000_000_001 + i * 2_000_000_000, 500_000_000_001 + i * 2_000_000_000
            raw.write(f'width=8,height=6,type=16,channels=1,pixel_depth=32,take_id={take},timestamp_utc_ns={utc},host_received_monotonic_ns={host},calibration=test,calibration_scale=1,calibration_offset_c=0,temperature_units=C,\n')
            pixels = (np.arange(48).reshape(6, 8) / 10 + 20 + i).astype(float)
            if nonfinite and i == 2:
                pixels[0, 0] = np.nan
            for row in pixels:
                raw.write(','.join(str(v) for v in row) + ',\n')
            writer.writerow([i, take, utc, host, 'test', 'video_parts/no_video.mp4', i])
            arrays.append(pixels)
    return arrays


def cages():
    return [{'mouse_id': 'WT_01', 'genotype': 'WT', 'roi': [0, 0, 4, 6]},
            {'mouse_id': 'WT_02', 'genotype': 'WT', 'roi': [4, 0, 4, 6]}]


def test_export_exact_pixels_metrics_and_timing(tmp_path):
    root = tmp_path / 'source'
    arrays = recording(root, 260, nonfinite=True)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
    config = roi.make_config(root, (6, 8), cages())
    out = roi.export_rois(root, config, tmp_path / 'output')
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['status'] == 'complete' and manifest['frames'] == 260
    assert manifest['scope'] == 'all_frames'
    assert manifest['processed_thermal_bytes_sha256'] == before['seek_thermal.csv']
    for cage in cages():
        x, y, w, h = cage['roi']
        chunks = list((out / cage['mouse_id'] / 'pixels').glob('*.npz'))
        assert len(chunks) == 2
        chunks.sort()
        offset = 0
        table = pd.read_csv(out / cage['mouse_id'] / 'temperature_timeseries.csv')
        for path in chunks:
            with np.load(path, allow_pickle=False) as saved:
                expected = np.array([a[y:y+h, x:x+w] for a in arrays[offset:offset+len(saved['pixels'])]])
                np.testing.assert_equal(saved['pixels'], expected)
                np.testing.assert_array_equal(saved['source_frame_index'], table.source_frame_index.iloc[offset:offset+len(expected)])
                np.testing.assert_array_equal(saved['timestamp_utc_ns'], table.timestamp_utc_ns.iloc[offset:offset+len(expected)])
                offset += len(expected)
        for i, arr in enumerate(arrays):
            crop = arr[y:y+h, x:x+w]
            if np.isfinite(crop).all():
                assert table.temperature_c[i] == crop.max()
                assert table.roi_mean_c[i] == pytest.approx(crop.mean())
                assert table.warmest_5pct_mean_c[i] == pytest.approx(crop[crop >= np.percentile(crop, 95)].mean())
            else:
                assert np.isnan(table.temperature_c[i]) and table.quality[i] == 'nonfinite_pixels'
        assert table.timestamp_utc_iso.iloc[0].endswith('.000000001Z')
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}


@pytest.mark.parametrize('box', [[-1, 0, 4, 6], [0, 0, 0, 6], [5, 0, 4, 6], [0, 0, 4.0, 6]])
def test_reject_invalid_roi(box):
    with pytest.raises(ValueError):
        roi.validate_cages([{'mouse_id': 'WT', 'genotype': 'WT', 'roi': box}], (6, 8))


def test_overlap_duplicate_names_and_configuration(tmp_path):
    root = tmp_path / 'source'
    recording(root)
    selection = cages()
    selection[1]['roi'][0] = 3
    with pytest.raises(ValueError, match='overlap'):
        roi.validate_cages(selection, (6, 8))
    selection = cages()
    selection[1]['mouse_id'] = 'wt_01'
    with pytest.raises(ValueError, match='unique'):
        roi.validate_cages(selection, (6, 8))
    config = roi.make_config(root, (6, 8), cages())
    path = tmp_path / 'roi.json'
    roi.atomic_json(path, config)
    assert roi.load_config(path, root, (6, 8)) == (config, False)
    other = tmp_path / 'other'
    recording(other)
    assert roi.load_config(path, other, (6, 8))[1]
    with pytest.raises(ValueError, match='dimensions'):
        roi.load_config(path, root, (7, 8))


@pytest.mark.parametrize('failure', ['truncated', 'log', 'resolution', 'write', 'cancel'])
def test_incomplete_runs_never_report_success(tmp_path, monkeypatch, failure):
    root = tmp_path / 'source'
    recording(root)
    raw = root / 'seek_thermal.csv'
    if failure == 'truncated':
        raw.write_bytes(raw.read_bytes()[:-100])
    elif failure == 'log':
        path = root / 'seek_log.csv'
        path.write_text(path.read_text().replace('500000000001', '500000000002'))
    elif failure == 'resolution':
        data = raw.read_text()
        position = data.index('width=8', 1)
        raw.write_text(data[:position] + data[position:].replace('width=8', 'width=7', 1))
    elif failure == 'write':
        import thermal_roi_storage
        monkeypatch.setattr(thermal_roi_storage, 'write_verified_archive', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    config = roi.make_config(root, (6, 8), cages())
    cancel = threading.Event()
    def progress(count, fraction, eta):
        if failure == 'cancel' and count == 2:
            cancel.set()
    with pytest.raises((ValueError, OSError, roi.ExportCancelled)):
        roi.export_rois(root, config, tmp_path / 'out', cancel, progress)
    manifest = json.loads(next((tmp_path / 'out').glob('*/manifest.json')).read_text())
    assert manifest['status'] == ('cancelled' if failure == 'cancel' else 'incomplete')


def test_sample_exports_are_explicit(tmp_path):
    root = tmp_path / 'source'
    recording(root)
    out = roi.export_rois(root, roi.make_config(root, (6, 8), cages()), tmp_path / 'out', max_frames=2)
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['scope'] == 'sample' and manifest['frames'] == 2


def test_fast_parser_matches_general_floats():
    arr = roi.parse_pixels([b'20.1,99.9,\r\n', b'10.0,11.2,\n'], 2)
    np.testing.assert_equal(arr, [[20.1, 99.9], [10, 11.2]])
    np.testing.assert_equal(roi.parse_pixels([b'-1.25,1e2,\n'], 2), [[-1.25, 100.]])
    with pytest.raises(ValueError):
        roi.parse_pixels([b'20.0,bad,\n'], 2)


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('size,reverse', [((600, 600), False), ((900, 350), True)])
def test_canvas_native_coordinate_mapping(app, size, reverse):
    from PySide6 import QtCore as C, QtTest
    from thermal_roi_gui import CageCanvas
    canvas = CageCanvas()
    canvas.resize(*size)
    canvas.set_image(np.zeros((60, 80, 3), dtype=np.uint8))
    canvas.show()
    app.processEvents()
    chosen = []
    canvas.changed.connect(lambda i, box: chosen.append(box))
    def display(x, y):
        r = canvas.image_rect()
        return C.QPoint(round(r.x() + (x+.5) * r.width()/80), round(r.y() + (y+.5)*r.height()/60))
    left, right = display(0, 0), display(79, 59)
    if reverse:
        left, right = right, left
    canvas.drawing = True
    QtTest.QTest.mousePress(canvas, C.Qt.MouseButton.LeftButton, pos=left)
    QtTest.QTest.mouseRelease(canvas, C.Qt.MouseButton.LeftButton, pos=right)
    assert chosen == [[0, 0, 80, 60]]
    canvas.close()


def test_window_selection_preview_and_overlap(app, tmp_path):
    from thermal_roi_gui import RoiWindow, inspect_recording
    root = tmp_path / 'source'
    recording(root)
    window = RoiWindow()
    window.recording_loaded(inspect_recording(root))
    window.change_roi(-1, [0, 0, 4, 6])
    window.change_roi(-1, [4, 0, 4, 6])
    assert [c['mouse_id'] for c in window.cages] == ['WT_01', 'WT_02']
    window.change_roi(1, [3, 0, 4, 6])
    assert window.cages[1]['roi'] == [4, 0, 4, 6]
    assert 'overlap' in window.status.text()
    assert not window.crop.pixmap().isNull()
    assert '4 × 6 native pixels' in window.crop_caption.text()
    window.close()


def test_native_browser_uses_selected_frame_and_cached_offsets(tmp_path):
    from thermal_roi_gui import NativeBrowser, inspect_recording
    root = tmp_path / 'source'
    arrays = recording(root)
    _, frame, entries = inspect_recording(root)
    browser = NativeBrowser(root, frame.pixels.shape)
    cancel = threading.Event()
    # Use a visibly different spatial pattern, not just a temperature offset.
    raw = root / 'seek_thermal.csv'
    text = raw.read_text()
    text = text.replace('24.0,24.1,24.2,24.3,24.4,24.5,24.6,24.7,',
                        '64.0,24.1,24.2,24.3,24.4,24.5,24.6,24.7,')
    raw.write_text(text)
    browser = NativeBrowser(root, frame.pixels.shape)
    frames = list(roi.iter_frames(root))
    for position in [4, 0, 2, 4]:
        np.testing.assert_equal(browser.read(position, entries[position], cancel), roi.colorize(frames[position].pixels))
    assert len(browser.offsets) == 6
    bad = list(entries[2])
    bad[3] = '1'
    with pytest.raises(ValueError, match='timestamp'):
        browser.read(2, bad, cancel)
    cancel.set()
    with pytest.raises(roi.ExportCancelled):
        browser.read(0, entries[0], cancel)


def test_native_button_loads_slider_frame(app, tmp_path):
    import time
    from thermal_roi_gui import RoiWindow, inspect_recording
    root = tmp_path / 'source'
    recording(root)
    window = RoiWindow()
    window.recording_loaded(inspect_recording(root))
    window.slider.setValue(3)
    window.native_preview()
    deadline = time.monotonic() + 5
    while window.thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert window.thread is None
    assert 'Native thermal frame 3' in window.preview_label.text()
    window.close()


def test_canvas_move_resize_and_letterbox(app):
    from PySide6 import QtCore as C, QtTest
    from thermal_roi_gui import CageCanvas
    canvas = CageCanvas()
    canvas.resize(800, 800)
    canvas.set_image(np.zeros((60, 80, 3), dtype=np.uint8))
    canvas.cages = [{'mouse_id': 'WT', 'genotype': 'WT', 'roi': [10, 10, 20, 20]}]
    canvas.active = 0
    canvas.show()
    app.processEvents()
    boxes = []
    canvas.changed.connect(lambda i, box: boxes.append(box))
    def point(x, y):
        r = canvas.image_rect()
        return C.QPoint(round(r.x() + (x+.5)*r.width()/80), round(r.y() + (y+.5)*r.height()/60))
    QtTest.QTest.mousePress(canvas, C.Qt.MouseButton.LeftButton, pos=point(20, 20))
    QtTest.QTest.mouseRelease(canvas, C.Qt.MouseButton.LeftButton, pos=point(25, 24))
    assert boxes[-1] == [15, 14, 20, 20]
    # Resize from the bottom-right handle while keeping the opposite corner fixed.
    rect = canvas.mapped(canvas.cages[0]['roi'])
    QtTest.QTest.mousePress(canvas, C.Qt.MouseButton.LeftButton, pos=rect.bottomRight().toPoint())
    QtTest.QTest.mouseRelease(canvas, C.Qt.MouseButton.LeftButton, pos=point(39, 34))
    assert boxes[-1] == [10, 10, 30, 25]
    canvas.drawing = True
    count = len(boxes)
    QtTest.QTest.mousePress(canvas, C.Qt.MouseButton.LeftButton, pos=C.QPoint(10, 10))
    QtTest.QTest.mouseRelease(canvas, C.Qt.MouseButton.LeftButton, pos=C.QPoint(20, 20))
    assert len(boxes) == count
    canvas.close()


def test_worker_completion_and_failure(app, tmp_path):
    import time
    from thermal_roi_gui import RoiWindow
    window = RoiWindow()
    received = []
    window.run_task(lambda _: 42, received.append)
    deadline = time.monotonic() + 5
    while window.thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert window.thread is None and received == [42]
    assert window.export_button.isEnabled()
    def fail(_):
        raise ValueError('Test failure')
    window.run_task(fail, received.append)
    deadline = time.monotonic() + 5
    while window.thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert window.thread is None and window.status.text() == 'Test failure'
    window.close()


def test_export_explains_unchecked_review_and_cancelled_folder(app, tmp_path, monkeypatch):
    from thermal_roi_gui import RoiWindow, inspect_recording, W
    root = tmp_path / 'source'
    recording(root)
    window = RoiWindow()
    window.recording_loaded(inspect_recording(root))
    window.change_roi(-1, [0, 0, 4, 6])
    calls = []
    monkeypatch.setattr(W.QFileDialog, 'getExistingDirectory', lambda *a: calls.append(True) or '')
    window.export()
    assert window.thread is None and not calls
    assert 'Export not running' in window.export_status.text()
    assert 'check' in window.export_status.text()
    window.review.setChecked(True)
    assert 'Ready to export' in window.export_status.text()
    window.export()
    assert calls and window.thread is None
    assert 'no output folder' in window.export_status.text()
    window.close()


@pytest.mark.parametrize('outcome', ['complete', 'error', 'cancel'])
def test_export_state_visible_through_worker_lifecycle(app, tmp_path, outcome):
    import time
    from thermal_roi_gui import RoiWindow
    window = RoiWindow()
    release = threading.Event()
    def job(worker):
        assert release.wait(5)
        if outcome == 'error':
            raise OSError('disk full')
        if outcome == 'cancel':
            raise roi.ExportCancelled()
        return tmp_path
    window.run_task(job, window.export_finished, exporting=True)
    try:
        assert 'EXPORT RUNNING' in window.export_status.text()
        assert window.cancel_button.isEnabled()
        assert not window.export_button.isEnabled()
        window.show_progress(20, .025, 60)
        assert '20 frames' in window.export_status.text()
        assert window.progress_bar.format() == '2.5% exported'
        if outcome == 'cancel':
            window.cancel_export()
            assert 'Stopping export' in window.export_status.text()
        release.set()
        deadline = time.monotonic() + 5
        while window.thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert window.thread is None
        expected = {'complete': 'EXPORT COMPLETE', 'error': 'Export failed', 'cancel': 'Export cancelled'}[outcome]
        assert expected in window.export_status.text()
        assert not window.export_timer.isActive()
        assert window.export_button.isEnabled()
    finally:
        release.set()
        window.close()


def test_recording_changed_before_export_is_rejected(tmp_path):
    root = tmp_path / 'source'
    recording(root)
    config = roi.make_config(root, (6, 8), cages())
    with (root / 'seek_log.csv').open('a') as handle:
        handle.write('\n')
    with pytest.raises(ValueError, match='changed'):
        roi.export_rois(root, config, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_archive_verifies_frame_ids_before_publication(tmp_path, monkeypatch):
    from thermal_roi_storage import write_verified_archive
    original = np.lib.format.write_array
    def corrupt(handle, array, **kwargs):
        if array.dtype == np.int64:
            array = array + 1
        return original(handle, array, **kwargs)
    monkeypatch.setattr(np.lib.format, 'write_array', corrupt)
    target = tmp_path / 'part.npz'
    with pytest.raises(OSError, match='verification failed'):
        write_verified_archive(target, {'pixels': np.zeros((1, 2, 2)),
                                        'source_frame_index': np.array([4], dtype=np.int64)}, lambda: None)
    assert not target.exists()


def test_cancellation_during_compression_prevents_complete_export(tmp_path, monkeypatch):
    import thermal_roi_storage
    root = tmp_path / 'source'
    recording(root)
    cancel = threading.Event()
    original = thermal_roi_storage.write_verified_archive
    def stop_after_first_archive(*args):
        result = original(*args)
        cancel.set()
        return result
    monkeypatch.setattr(thermal_roi_storage, 'write_verified_archive', stop_after_first_archive)
    with pytest.raises(roi.ExportCancelled):
        roi.export_rois(root, roi.make_config(root, (6, 8), cages()), tmp_path / 'out', cancel)
    manifest = json.loads(next((tmp_path / 'out').glob('*/manifest.json')).read_text())
    assert manifest['status'] == 'cancelled'


def test_one_failed_archive_worker_cannot_publish_complete_run(tmp_path, monkeypatch):
    import thermal_roi_storage
    root = tmp_path / 'source'
    recording(root)
    original = thermal_roi_storage.write_verified_archive
    def fail_one(target, arrays, check_cancel):
        if 'WT_02' in str(target):
            raise OSError('second cage write failed')
        return original(target, arrays, check_cancel)
    monkeypatch.setattr(thermal_roi_storage, 'write_verified_archive', fail_one)
    with pytest.raises(OSError, match='second cage'):
        roi.export_rois(root, roi.make_config(root, (6, 8), cages()), tmp_path / 'out')
    manifest = json.loads(next((tmp_path / 'out').glob('*/manifest.json')).read_text())
    assert manifest['status'] == 'incomplete'
    assert any('WT_01' in p and p.endswith('.npz') for p in manifest['outputs'])


@pytest.mark.parametrize('selection', [
    [{'mouse_id': 'single', 'genotype': 'WT', 'roi': [0, 0, 8, 6]}],
    [{'mouse_id': f'mouse_{i}', 'genotype': 'WT', 'roi': [i*2, 0, 2, 6]} for i in range(4)],
])
def test_exports_one_or_many_cages(tmp_path, selection):
    root = tmp_path / 'source'
    arrays = recording(root)
    out = roi.export_rois(root, roi.make_config(root, (6, 8), selection), tmp_path / 'out')
    for cage in selection:
        x, y, w, h = cage['roi']
        with np.load(out / cage['mouse_id'] / 'pixels/part_000001.npz', allow_pickle=False) as saved:
            np.testing.assert_equal(saved['pixels'], np.array(arrays)[:, y:y+h, x:x+w])
