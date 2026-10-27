"""Room calibration through the same writer used by real and demo cameras."""
import csv
import json
import time

import numpy as np
import pytest

from camera_demo import DemoCameraCore
from recording_coordinator import RecordingCoordinator
from seek_calibration import get_seek_calibration

pytestmark = pytest.mark.usefixtures('example_calibration')


@pytest.mark.parametrize('camera_c,true_c', [
    (-20, -21.5), (0, 2.5),
    (25, 32.5), (30, 38.5), (100, 122.5),
])
def test_example_calibration_converts_celsius(camera_c, true_c):
    assert get_seek_calibration('example-calibration').apply(camera_c) == pytest.approx(true_c, abs=1e-10)


def test_calibration_handles_pixel_arrays_without_changing_camera_values():
    camera_c = np.array([[-20, 0, 25], [30, 100, np.nan]], dtype=np.float32)
    original = camera_c.copy()
    actual = get_seek_calibration('example-calibration').apply(camera_c)
    np.testing.assert_allclose(actual, [[-21.5, 2.5, 32.5],
                                       [38.5, 122.5, np.nan]], atol=1e-5)
    np.testing.assert_array_equal(camera_c, original)
    np.testing.assert_array_equal(get_seek_calibration('none').apply(camera_c), original)
    assert get_seek_calibration('none').apply(25.123456) == 25.123456


def test_unknown_calibration_is_rejected_without_changing_options():
    controller = RecordingCoordinator(DemoCameraCore())
    with pytest.raises(ValueError, match='Unknown Seek calibration'):
        controller.configure(seek_calibration='unknown-room')
    assert controller.options['seek_calibration'] == 'none'


@pytest.mark.parametrize('sample_mode', ['custom', 'all', 'after_shutter'])
def test_each_take_saves_selected_calibration_and_metadata(tmp_path, sample_mode):
    core = DemoCameraCore()
    core.seek_connected.set()
    controller = RecordingCoordinator(core)
    controller.cameras_ready = True
    controller.seek_ready = True
    controller.flir_cams = [dict(index=0, serial='DEMO', cam=None)]
    controller.on_flir(0, 0, 0, time.time(), np.zeros((60, 80), dtype=np.uint8), time.perf_counter_ns())
    controller.configure(output=str(tmp_path), sample_mode=sample_mode)
    if sample_mode == 'after_shutter':
        controller.apply_shutter('Custom', 1)
    camera_c = np.array([[-20, 0, 25], [30, 100, 20]], dtype=np.float32)
    original = camera_c.copy()
    selections = ('example-calibration', 'none')
    try:
        for take_id, name in enumerate(selections, 1):
            controller.configure(seek_calibration=name)
            assert controller.request('START')['ok']
            with pytest.raises(RuntimeError, match='Finish the current take'):
                controller.configure(seek_calibration='none')
            if sample_mode == 'after_shutter':
                controller.shutter_tick()
            header = dict(timestamp_utc_ns=time.time_ns(), fpa_frame_count=take_id, width=3, height=2)
            controller.on_seek((time.time(), np.zeros((2, 3, 4), dtype=np.uint8),
                                camera_c, header, time.perf_counter_ns()))
            controller.on_flir(0, take_id, take_id, time.time(),
                               np.zeros((60, 80), dtype=np.uint8), time.perf_counter_ns())
            assert controller.request('STOP')['ok']
            assert controller.finished.wait(5)
            report = json.loads((controller.run_dir / f'take{take_id:02d}_sync.json').read_text())
            assert report['status'] == 'complete', report['errors']
            assert report['frame_counts']['seek'] == 1
            assert report['seek_calibration'] == dict(
                name=name, scale=1.2 if take_id == 1 else 1.0,
                offset_c=2.5 if take_id == 1 else 0.0, input_units='C', output_units='C')
            assert 'calibration' not in header
            np.testing.assert_array_equal(camera_c, original)

        with (controller.run_dir / 'seek_thermal.csv').open(newline='') as handle:
            rows = list(csv.reader(handle))
        assert len(rows) == 6
        assert 'calibration=example-calibration' in rows[0]
        assert 'calibration_scale=1.2' in rows[0]
        assert 'calibration_offset_c=2.5' in rows[0]
        assert 'temperature_units=C' in rows[0]
        assert rows[1] == ['-21.5', '2.5', '32.5', '']
        assert rows[2] == ['38.5', '122.5', '26.5', '']
        assert 'calibration=none' in rows[3]
        assert 'calibration_scale=1.0' in rows[3]
        assert 'calibration_offset_c=0.0' in rows[3]
        assert rows[4] == ['-20.0', '0.0', '25.0', '']
        assert rows[5] == ['30.0', '100.0', '20.0', '']
        with (controller.run_dir / 'seek_log.csv').open(newline='') as handle:
            logs = list(csv.DictReader(handle))
        assert [row['calibration'] for row in logs] == list(selections)
        assert all((controller.run_dir / row['file']).is_file() for row in logs)
    finally:
        controller.request('STOP')
        assert controller.finished.wait(5)
        controller.shutdown()
        if controller.writer:
            controller.writer.join(5)
            assert not controller.writer.is_alive()
