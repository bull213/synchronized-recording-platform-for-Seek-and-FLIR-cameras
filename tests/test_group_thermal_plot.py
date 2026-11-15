import csv
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import pytest

from ultramic_studio.recorder.group_thermal_plot import (
    DEFAULT_PLOT_SETTINGS, _mouse_profile, _read_trace, align_mouse_data,
    compute_group_analysis, create_group_plot, draw_group_figure, load_mouse_csv,
    one_second_grid, parse_time_str,
)
from test_laptop_layout import window


def write_seek(path, *, start_ns, frames, calibrated=True, step_ns=1_000_000_000):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        for index, frame in enumerate(frames):
            stamp = start_ns + index * step_ns
            metadata = [f'width={frame.shape[1]}', f'height={frame.shape[0]}',
                        f'timestamp_utc_ns={stamp}']
            if calibrated:
                metadata += ['temperature_units=C', 'calibration=test',
                             'calibration_scale=1', 'calibration_offset_c=0']
            writer.writerow(metadata)
            writer.writerows(frame)
        return


def config(**updates):
    result = dict(DEFAULT_PLOT_SETTINGS)
    result['ROI_SIZE_PIXELS'] = 1
    result.update(updates)
    return result


def test_calibrated_csv_is_not_calibrated_twice(tmp_path):
    path = tmp_path / 'mouse.csv'
    write_seek(path, start_ns=1_800_000_000_000_000_000,
               frames=[np.full((3, 3), 30.0), np.full((3, 3), 31.0)])
    trace = _read_trace(path, config())
    np.testing.assert_allclose(trace.temperatures_c, [30, 31])
    assert trace.elapsed_s.tolist() == [0, 1]


def test_legacy_raw_csv_uses_configured_calibration(tmp_path):
    path = tmp_path / 'raw.csv'
    write_seek(path, start_ns=1_800_000_000_000_000_000,
               frames=[np.full((3, 3), 20.0), np.full((3, 3), 21.0)], calibrated=False)
    trace = _read_trace(path, config())
    np.testing.assert_allclose(trace.temperatures_c, [0.84247 * 20 + 4.7243,
                                                       0.84247 * 21 + 4.7243])


def test_group_plot_bins_different_sampling_grids_and_durations(tmp_path):
    start = 1_800_000_000_000_000_000
    a, b = tmp_path / 'a.csv', tmp_path / 'b.csv'
    frames = [np.full((3, 3), 30.0) for _ in range(600)]
    write_seek(a, start_ns=start, frames=frames)
    write_seek(b, start_ns=start + 1_000_000_000, frames=frames, step_ns=2_000_000_000)
    cfg = config(OUTPUT_DIR=str(tmp_path), BIN_MINUTES=1, PLOT_HOURS=1,
                 MIN_SAMPLES_PER_MINUTE=30, MINUTE_COVERAGE_FRACTION=.8)
    groups = {'WT': ['a.csv', 'b.csv']}
    output = create_group_plot(tmp_path, groups, settings=cfg)
    assert output.is_file()
    traces = [_read_trace(a, cfg), _read_trace(b, cfg)]
    first = _mouse_profile(traces[0], cfg)['profile']
    second = _mouse_profile(traces[1], cfg)['profile']
    assert np.isfinite(first[:10]).all() and np.isnan(first[10:]).all()
    assert np.isfinite(second[:20]).all() and np.isnan(second[20:]).all()


def test_recordings_same_local_clock_time_on_different_dates_pass_check(tmp_path):
    from ultramic_studio.recorder.group_thermal_plot import _validate_start_phases, MouseTrace

    local = ZoneInfo('Asia/Jerusalem')
    starts = [datetime(2026, 1, 1, 19, 0, tzinfo=local), datetime(2026, 1, 3, 19, 20, tzinfo=local)]
    traces = [MouseTrace(f'mouse-{i}.csv', np.array([i, i + 1]), np.array([0., 1.]),
                         np.array([30., 31.]), stamp.astimezone(timezone.utc))
              for i, stamp in enumerate(starts)]
    _validate_start_phases({'WT': traces}, config(MAX_START_TIME_DIFF_MINUTES=30))


def test_recordings_outside_clock_phase_tolerance_are_rejected():
    from ultramic_studio.recorder.group_thermal_plot import _validate_start_phases, MouseTrace

    local = ZoneInfo('Asia/Jerusalem')
    starts = [datetime(2026, 1, 1, 19, 0, tzinfo=local), datetime(2026, 1, 1, 20, 1, tzinfo=local)]
    traces = [MouseTrace(f'mouse-{i}.csv', np.array([i, i + 1]), np.array([0., 1.]),
                         np.array([30., 31.]), stamp.astimezone(timezone.utc))
              for i, stamp in enumerate(starts)]
    with pytest.raises(ValueError, match='local clock phases differ'):
        _validate_start_phases({'WT': traces}, config(MAX_START_TIME_DIFF_MINUTES=60))


def test_group_plot_exports_png_for_aligned_group(tmp_path):
    start = 1_800_000_000_000_000_000
    a, b = tmp_path / 'a.csv', tmp_path / 'b.csv'
    frames_a = [np.full((3, 3), 30.0 + (index % 2)) for index in range(60)]
    frames_b = [np.full((3, 3), 34.0 + (index % 2)) for index in range(60)]
    write_seek(a, start_ns=start, frames=frames_a)
    write_seek(b, start_ns=start + 2_000_000_000, frames=frames_b)
    cfg = config(OUTPUT_DIR=str(tmp_path), OUTPUT_FILENAME='group.png',
                 MAX_START_TIME_DIFF_MINUTES=60, BIN_MINUTES=1, PLOT_HOURS=1)
    output = create_group_plot(tmp_path, {'WT': ['a.csv', 'b.csv']}, settings=cfg)
    assert output.name == 'group.png'
    assert output.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')


def test_profile_uses_recording_local_clock_for_light_dark_summary(tmp_path):
    start = datetime(2026, 1, 1, 6, 30, tzinfo=ZoneInfo('Asia/Jerusalem'))
    start_ns = int(start.timestamp() * 1e9)
    frames = ([np.full((3, 3), 30.0) for _ in range(1800)]
              + [np.full((3, 3), 32.0) for _ in range(1800)])
    path = tmp_path / 'phase.csv'
    write_seek(path, start_ns=start_ns, frames=frames)
    cfg = config(BIN_MINUTES=1, PLOT_HOURS=1)
    profile = _mouse_profile(_read_trace(path, cfg), cfg)
    assert profile['dark_minus_light'] == pytest.approx(-2.0)


def test_plot_action_is_available_without_camera_dependency(window):
    from PySide6 import QtGui

    _, recorder = window
    actions = recorder.findChildren(QtGui.QAction)
    plot_action = next(action for action in actions if action.text() == 'Multi-mouse thermal plot…')
    assert plot_action.isEnabled()


def test_plot_dialog_saves_group_and_settings(tmp_path):
    from PySide6 import QtCore, QtWidgets
    from ultramic_studio.recorder.group_thermal_dialog import GroupThermalPlotDialog

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    preferences = QtCore.QSettings(str(tmp_path / 'settings.ini'), QtCore.QSettings.Format.IniFormat)
    dialog = GroupThermalPlotDialog(None, preferences, tmp_path)
    dialog.table.item(0, 0).setText('WT')
    dialog.table.item(0, 1).setText('mouse-a.csv; mouse-b.csv')
    dialog._accept()
    assert dialog.result_data['groups'] == {'WT': ['mouse-a.csv', 'mouse-b.csv']}
    assert preferences.value(dialog.SETTINGS_KEY)
    dialog.close()
    app.processEvents()


def test_load_mouse_csv_various_schemas(tmp_path):
    # Schema 1: timestamp_utc_iso and roi_max_c with mouse_id
    p1 = tmp_path / 'm1.csv'
    p1.write_text('timestamp_utc_iso,roi_max_c,mouse_id\n2026-11-03T05:00:00Z,27.5,WT_01\n2026-11-03T05:00:01Z,27.8,WT_01\n')
    df1 = load_mouse_csv(p1)
    assert len(df1) == 2
    assert df1['mouse_id'].iloc[0] == 'WT_01'
    np.testing.assert_allclose(df1['temperature_c'], [27.5, 27.8])

    # Schema 2: timestamp_utc_ns and temperature_c
    p2 = tmp_path / 'm2.csv'
    p2.write_text('timestamp_utc_ns,temperature_c\n1793682000000000000,31.2\n1793682001000000000,31.4\n')
    df2 = load_mouse_csv(p2)
    assert len(df2) == 2
    assert df2['mouse_id'].iloc[0] == 'm2'
    np.testing.assert_allclose(df2['temperature_c'], [31.2, 31.4])

    # Schema 3: generic time and temp_c
    p3 = tmp_path / 'm3.csv'
    p3.write_text('time,temp_c\n2026-10-21 14:00:00,29.1\n2026-10-21 14:00:01,29.2\n')
    df3 = load_mouse_csv(p3)
    assert len(df3) == 2
    np.testing.assert_allclose(df3['temperature_c'], [29.1, 29.2])


def test_start_time_alignment_5am(tmp_path):
    # Create timestamps starting on Nov 2 at 18:00 Israel time (16:00 UTC) through Nov 4 08:00
    times = pd.date_range('2026-11-02 18:00:00+02:00', '2026-11-04 08:00:00+02:00', freq='1s')
    temps = np.full(len(times), 30.0)
    df = pd.DataFrame({'datetime_utc': times.tz_convert('UTC'), 'temperature_c': temps, 'mouse_id': 'm1'})

    sec, temp, start_ts = align_mouse_data(df, start_time='05:00', window_hours=24, timezone_name='Asia/Jerusalem')
    assert start_ts == pd.Timestamp('2026-11-03 05:00:00+02:00')
    assert len(sec) == 86400
    assert sec[0] == 0.0
    assert sec[-1] == 86399.0


def test_48h_time_window_multi_day(tmp_path):
    # 50 hours of data to test 48-hour window
    times = pd.date_range('2026-11-02 04:00:00+02:00', '2026-11-04 10:00:00+02:00', freq='1s')
    # Day 1 light: 28C, Day 1 dark: 30C, Day 2 light: 28C, Day 2 dark: 30C
    temps = np.full(len(times), 29.0)
    p = tmp_path / 'mouse_48h.csv'
    pd.DataFrame({'timestamp_utc_iso': times.tz_convert('UTC').strftime('%Y-%m-%dT%H:%M:%SZ'),
                  'temperature_c': temps, 'mouse_id': 'WT_48h'}).to_csv(p, index=False)

    cfg = {
        'START_TIME': '05:00',
        'TIME_WINDOW_HOURS': 48,
        'LIGHTS_ON_TIME': '07:00',
        'LIGHTS_OFF_TIME': '19:00',
        'TIMEZONE': 'Asia/Jerusalem',
        'BIN_MINUTES': 30,
        'OUTPUT_DIR': str(tmp_path),
        'OUTPUT_FILENAME': 'summary_48h.png',
    }
    out_png = create_group_plot(tmp_path, {'WT': [p.name]}, settings=cfg)
    assert out_png.is_file()
    assert out_png.stat().st_size > 1000

    timeseries_csv = tmp_path / 'extracted_temperature_timeseries.csv'
    assert timeseries_csv.is_file()
    res_df = pd.read_csv(timeseries_csv)
    # 48 hours with 30-min bins = 96 bins
    assert len(res_df) == 96
    np.testing.assert_allclose(res_df['temperature_change_c'], 0.0)


def test_custom_light_dark_cycle(tmp_path):
    # Custom light cycle: Lights ON 05:00, Lights OFF 17:00
    times = pd.date_range('2026-11-03 05:00:00+02:00', '2026-11-04 05:00:00+02:00', freq='1s', inclusive='left')
    p = tmp_path / 'mouse_custom_cycle.csv'
    # First 12h: Light phase (30.0C), second 12h: Dark phase (33.0C)
    temps = np.where(times.hour.isin(range(5, 17)), 30.0, 33.0)
    pd.DataFrame({'timestamp_utc_iso': times.tz_convert('UTC').strftime('%Y-%m-%dT%H:%M:%SZ'),
                  'temperature_c': temps, 'mouse_id': 'Custom_Mouse'}).to_csv(p, index=False)

    cfg = {
        'START_TIME': '05:00',
        'TIME_WINDOW_HOURS': 24,
        'LIGHTS_ON_TIME': '05:00',
        'LIGHTS_OFF_TIME': '17:00',
        'TIMEZONE': 'Asia/Jerusalem',
        'OUTPUT_DIR': str(tmp_path),
    }
    res = compute_group_analysis(tmp_path, {'Custom': [p.name]}, settings=cfg)
    diff = res['paired'].loc['Custom', 'decrease'].iloc[0]
    # Dark (33) - Light (30) = +3.0 C
    assert diff == pytest.approx(3.0, abs=1e-4)


def test_draw_group_figure_multi_group(tmp_path):
    times = pd.date_range('2026-11-03 07:00:00+02:00', '2026-11-04 07:00:00+02:00', freq='1s', inclusive='left')
    files = {}
    for name, mean_t in [('WT', 30.0), ('Dravet', 32.0), ('KO', 34.0)]:
        p = tmp_path / f'{name}.csv'
        pd.DataFrame({'timestamp_utc_iso': times.tz_convert('UTC').strftime('%Y-%m-%dT%H:%M:%SZ'),
                      'temperature_c': np.full(len(times), mean_t), 'mouse_id': name}).to_csv(p, index=False)
        files[name] = [p]

    cfg = {
        'START_TIME': '07:00',
        'TIME_WINDOW_HOURS': 24,
        'OUTPUT_DIR': str(tmp_path),
        'OUTPUT_FILENAME': 'three_groups.png',
    }
    out_png = create_group_plot(None, files, settings=cfg)
    assert out_png.is_file()
    assert out_png.stat().st_size > 1000


def test_reproduce_moran_wt2_vs_dravet_numerical_parity():
    inputs = Path('mouse_thermal_results/02_analyses/20261104_moran_WT2_vs_Dravet_24h/analysis_inputs')
    if not (inputs / 'WT_01.csv').is_file() or not (inputs / 'Dravet_01.csv').is_file():
        pytest.skip('Analysis inputs not found')

    groups = {
        'WT': [inputs / 'WT_01.csv', inputs / 'WT_02.csv'],
        'Dravet': [inputs / 'Dravet_01.csv']
    }
    settings = {
        'START_TIME': '07:00',
        'TIME_WINDOW_HOURS': 24,
        'LIGHTS_ON_TIME': '07:00',
        'LIGHTS_OFF_TIME': '19:00',
        'TIMEZONE': 'Asia/Jerusalem',
        'MATCH_COMMON_SUPPORT': True,
        'BIN_MINUTES': 30,
    }
    results = compute_group_analysis(None, groups, settings=settings)
    contrasts = results['paired']['decrease'].groupby('genotype').mean().to_dict()

    # Exact parity with van der Vinne / Moran WT2 vs Dravet baseline
    assert contrasts['WT'] == pytest.approx(0.45638888, abs=1e-4)
    assert contrasts['Dravet'] == pytest.approx(0.66277777, abs=1e-4)
    assert results['mask'].sum() == 84350


def test_dialog_new_settings_controls(tmp_path):
    from PySide6 import QtCore, QtWidgets
    from ultramic_studio.recorder.group_thermal_dialog import GroupThermalPlotDialog

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    pref_file = tmp_path / 'settings.ini'
    preferences = QtCore.QSettings(str(pref_file), QtCore.QSettings.Format.IniFormat)

    dialog = GroupThermalPlotDialog(None, preferences, tmp_path)
    dialog.start_time.setTime(QtCore.QTime(5, 0))
    dialog.time_window.setValue(48)
    dialog.lights_on.setTime(QtCore.QTime(6, 30))
    dialog.lights_off.setTime(QtCore.QTime(18, 30))
    dialog.table.item(0, 0).setText('Mutant')
    dialog.table.item(0, 1).setText('mouse1.csv')
    dialog._accept()

    assert dialog.result_data['START_TIME'] == '05:00'
    assert dialog.result_data['TIME_WINDOW_HOURS'] == 48
    assert dialog.result_data['LIGHTS_ON_TIME'] == '06:30'
    assert dialog.result_data['LIGHTS_OFF_TIME'] == '18:30'
    assert dialog.result_data['groups'] == {'Mutant': ['mouse1.csv']}

    # Verify persistence
    dialog2 = GroupThermalPlotDialog(None, preferences, tmp_path)
    assert dialog2.start_time.time().toString('HH:mm') == '05:00'
    assert dialog2.time_window.value() == 48
    assert dialog2.lights_on.time().toString('HH:mm') == '06:30'
    assert dialog2.lights_off.time().toString('HH:mm') == '18:30'
    dialog.close()
    dialog2.close()
    app.processEvents()
