"""Modular Seek thermal data analysis and publication-quality group figures.

Accepts 1-mouse-per-CSV Seek thermal files (e.g. from cage ROI export or full-frame max)
and creates the 3-panel publication figure:
  1. Light-dark temperature contrast
  2. Temperature profile across the configured time window with dynamic light/dark spans
  3. Temperature decrease during light (Dark minus Light °C)
"""
from __future__ import annotations

import csv
import json
import re
import warnings
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd

# Styling constants matching the publication reference figures
WT_COLOR = '#087E8B'
DRAVET_COLOR = '#CD684E'
INK = '#243247'
MUTED = '#627287'
LIGHT_BG = '#FFF4D8'
DARK_BG = '#E9EFF7'
BORDER = '#D2DAE4'
DIVIDER = '#A5B2C4'

PALETTE = [WT_COLOR, DRAVET_COLOR, '#4A7BB0', '#81B29A', '#E07A5F', '#F2CC8F', '#3D405B', '#9B5DE5']
LINE_STYLES = ['-', (0, (5, 2)), (0, (3, 1, 1, 1)), ':', (0, (5, 1))]

DEFAULT_PLOT_SETTINGS = {
    'START_TIME': '07:00',
    'TIME_WINDOW_HOURS': 24,
    'LIGHTS_ON_TIME': '07:00',
    'LIGHTS_OFF_TIME': '19:00',
    'TIMEZONE': 'Asia/Jerusalem',
    'BIN_MINUTES': 30,
    'MIN_SAMPLES_PER_MINUTE': 30,
    'MATCH_COMMON_SUPPORT': True,
    'OUTPUT_DIR': None,
    'OUTPUT_FILENAME': 'multi_mouse_thermal_summary.png',
    'DPI': 300,
    'VALID_TEMPERATURE_C': (-15.0, 65.0),
    'LEGACY_CALIBRATION': (0.84247, 4.7243),
    # Backwards-compatibility aliases
    'PLOT_HOURS': 24,
    'LIGHTS_ON_HOUR': 7,
    'LIGHTS_OFF_HOUR': 19,
    'ROI_SIZE_PIXELS': 7,
    'MAX_START_TIME_DIFF_MINUTES': 60,
}


@dataclass
class MouseTrace:
    filename: str
    timestamps_ns: np.ndarray
    elapsed_s: np.ndarray
    temperatures_c: np.ndarray
    start_utc: datetime
    mouse_id: str = ''


def parse_time_str(val: str | dt_time | int | float) -> dt_time:
    """Parse time string 'HH:MM' or 'HH:MM:SS', integer hour, or datetime.time."""
    if isinstance(val, dt_time):
        return val
    if isinstance(val, (int, float)):
        h = int(val) % 24
        m = int(round((val - int(val)) * 60)) % 60
        return dt_time(h, m)
    val_str = str(val).strip()
    parts = val_str.split(':')
    if len(parts) == 1:
        return dt_time(int(parts[0]) % 24, 0)
    if len(parts) >= 2:
        return dt_time(int(parts[0]) % 24, int(parts[1]) % 60, int(float(parts[2])) if len(parts) > 2 else 0)
    return dt_time(7, 0)


def _extract_hotspot(frame: np.ndarray, lower: float, upper: float, roi_size: int) -> float:
    """Extract mean temperature of a square patch centered on the hottest valid pixel."""
    valid = np.isfinite(frame) & (frame >= lower) & (frame <= upper)
    if not valid.any():
        return float('nan')
    height, width = frame.shape[:2]
    cy, cx = np.unravel_index(np.where(valid, frame, -np.inf).argmax(), frame.shape)
    side = min(roi_size, height, width)
    y0 = min(max(cy - side // 2, 0), height - side)
    x0 = min(max(cx - side // 2, 0), width - side)
    patch = frame[y0:y0 + side, x0:x0 + side]
    patch = patch[np.isfinite(patch) & (patch >= lower) & (patch <= upper)]
    return float(patch.mean()) if patch.size else float('nan')


def _load_legacy_seek_frames(path: Path, config=DEFAULT_PLOT_SETTINGS) -> pd.DataFrame:
    """Parse legacy Seek stacked raw 2D pixel CSV format if encountered."""
    header_re = re.compile(rb'(?:^|[,|])\s*(width|height|timestamp_utc_ns|temperature_units)\s*=\s*([^,|\r\n]+)')
    lower, upper = config.get('VALID_TEMPERATURE_C', (-15.0, 65.0))
    roi_size = int(config.get('ROI_SIZE_PIXELS', 7))
    timestamps, temperatures = [], []
    first_units = None

    with path.open('rb', buffering=4 * 1024 * 1024) as stream:
        while line := stream.readline():
            values = {m.group(1).decode('ascii'): m.group(2).strip().decode('ascii')
                      for m in header_re.finditer(line)}
            if not {'width', 'height', 'timestamp_utc_ns'} <= values.keys():
                continue
            width = int(values['width'])
            height = int(values['height'])
            stamp = int(values['timestamp_utc_ns'])
            units = values.get('temperature_units')
            frame = np.empty((height, width), dtype=np.float32)
            for y in range(height):
                row = stream.readline()
                frame[y] = np.fromstring(row.decode('ascii').strip().rstrip(',|'), sep=',')
            if first_units is None:
                first_units = units
            if units is None:
                scale, offset = config.get('LEGACY_CALIBRATION', (0.84247, 4.7243))
                frame = scale * frame + offset
            val = _extract_hotspot(frame, lower, upper, roi_size)
            timestamps.append(stamp)
            temperatures.append(val)

    stamps_arr = np.asarray(timestamps, dtype=np.int64)
    dt_utc = pd.to_datetime(stamps_arr, unit='ns', utc=True)
    return pd.DataFrame({
        'datetime_utc': dt_utc,
        'temperature_c': np.asarray(temperatures, dtype=float),
        'mouse_id': path.stem,
    })


def _resolve_npz_files(path: Path | str) -> list[Path]:
    """Find all matching .npz chunks for a given file or directory path."""
    p = Path(path)
    if p.is_dir():
        pixels_dir = p / 'pixels'
        target_dir = pixels_dir if pixels_dir.is_dir() else p
        chunks = sorted(target_dir.glob('part_*.npz'))
        if not chunks:
            chunks = sorted(target_dir.glob('*.npz'))
        return chunks
    if p.is_file() and p.suffix.lower() == '.npz':
        if p.name.startswith('part_'):
            chunks = sorted(p.parent.glob('part_*.npz'))
            if chunks:
                return chunks
        return [p]
    return []


def _load_npz_frames(path: Path | str, config=DEFAULT_PLOT_SETTINGS) -> pd.DataFrame:
    """Load temperature timeseries from .npz chunk archives."""
    chunks = _resolve_npz_files(path)
    if not chunks:
        raise FileNotFoundError(f'No .npz files found in {path}')

    p = Path(path)
    if p.is_dir():
        mouse_id = p.parent.name if p.name == 'pixels' else p.name
    elif p.parent.name == 'pixels':
        mouse_id = p.parent.parent.name
    else:
        mouse_id = p.stem

    lower, upper = config.get('VALID_TEMPERATURE_C', (-15.0, 65.0))
    roi_size = int(config.get('ROI_SIZE_PIXELS', 7))

    timestamps, temperatures = [], []
    for chunk_path in chunks:
        with np.load(chunk_path, allow_pickle=False) as data:
            pixels = data['pixels']
            ts = data['timestamp_utc_ns']
            n_frames = len(pixels)
            for i in range(n_frames):
                frame = pixels[i]
                val = _extract_hotspot(frame, lower, upper, roi_size)
                timestamps.append(int(ts[i]))
                temperatures.append(val)

    if not timestamps:
        raise ValueError(f'{path}: No thermal frames found in .npz archive(s).')

    stamps_arr = np.asarray(timestamps, dtype=np.int64)
    dt_utc = pd.to_datetime(stamps_arr, unit='ns', utc=True)
    df = pd.DataFrame({
        'datetime_utc': dt_utc,
        'temperature_c': np.asarray(temperatures, dtype=float),
        'mouse_id': mouse_id,
    })
    return df.sort_values('datetime_utc').drop_duplicates('datetime_utc').reset_index(drop=True)


def load_mouse_csv(path: Path | str, config=DEFAULT_PLOT_SETTINGS) -> pd.DataFrame:
    """Load a 1-mouse CSV or NPZ file/directory, autodetecting timestamp and temperature columns.

    Supports:
      - Cage ROI .npz chunks (single chunk, all parts, or mouse directory)
      - Cage ROI timeseries: timestamp_utc_iso / timestamp_utc_ns, roi_max_c / temperature_c
      - Dravet frame max / full frame max: timestamp_utc_iso, temperature_c
      - Generic single-mouse CSVs with timestamp and temperature columns
      - Legacy stacked seek frames
    """
    path = Path(path)
    if path.is_dir() or path.suffix.lower() == '.npz':
        if path.is_dir() and path.name != 'pixels' and (path / 'temperature_timeseries.csv').is_file():
            return load_mouse_csv(path / 'temperature_timeseries.csv', config)
        npz_files = _resolve_npz_files(path)
        if npz_files:
            return _load_npz_frames(path, config)
        if path.is_dir():
            ts_csv = path / 'temperature_timeseries.csv'
            if ts_csv.is_file():
                return load_mouse_csv(ts_csv, config)
        raise FileNotFoundError(f'No usable thermal files found in: {path}')

    if not path.is_file():
        raise FileNotFoundError(f'File not found: {path}')

    with path.open('rb') as f:
        first_line = f.readline()
        if b'width=' in first_line and b'height=' in first_line and b'timestamp_utc_ns=' in first_line:
            return _load_legacy_seek_frames(path, config)

    df = pd.read_csv(path)
    if df.empty:
        raise ValueError(f'{path.name}: CSV is empty.')

    # Detect timestamp column
    time_col = None
    time_candidates = ['timestamp_utc_iso', 'timestamp_utc_ns', 'timestamp_iso',
                       'timestamp', 'datetime', 'time', 'date_time', 'utc_time']
    for candidate in time_candidates:
        if candidate in df.columns:
            time_col = candidate
            break
    if time_col is None:
        for col in df.columns:
            if 'time' in col.lower() or 'date' in col.lower():
                time_col = col
                break
    if time_col is None:
        raise ValueError(f'{path.name}: Could not identify timestamp column among {list(df.columns)}')

    if time_col == 'timestamp_utc_ns' or pd.api.types.is_integer_dtype(df[time_col]):
        times = pd.to_datetime(df[time_col], unit='ns', utc=True)
    else:
        times = pd.to_datetime(df[time_col], utc=True)

    # Detect temperature column
    temp_col = None
    temp_candidates = ['temperature_c', 'roi_max_c', 'mouse_temp_c', 'temp_c',
                       'roi_mean_c', 'warmest_5pct_mean_c', 'temperature', 'temp']
    for candidate in temp_candidates:
        if candidate in df.columns:
            temp_col = candidate
            break
    if temp_col is None:
        for col in df.columns:
            if col != time_col and pd.api.types.is_numeric_dtype(df[col]):
                temp_col = col
                break
    if temp_col is None:
        raise ValueError(f'{path.name}: Could not identify temperature column among {list(df.columns)}')

    temps = pd.to_numeric(df[temp_col], errors='coerce').to_numpy(dtype=float)
    lower, upper = config.get('VALID_TEMPERATURE_C', (-15.0, 65.0))
    valid = np.isfinite(temps) & (temps >= lower) & (temps <= upper)
    temps[~valid] = np.nan

    mouse_id = str(df['mouse_id'].iloc[0]) if 'mouse_id' in df.columns else path.stem
    result = pd.DataFrame({
        'datetime_utc': times,
        'temperature_c': temps,
        'mouse_id': mouse_id,
    })
    result = result.sort_values('datetime_utc').reset_index(drop=True)
    return result


load_mouse_data = load_mouse_csv


def _read_trace(path: Path, config=DEFAULT_PLOT_SETTINGS) -> MouseTrace:
    """Helper preserving MouseTrace interface for test backwards-compatibility."""
    df = load_mouse_csv(path, config)
    stamps = df.datetime_utc.astype('int64').to_numpy()
    elapsed = (stamps - stamps[0]) / 1e9
    start = datetime.fromtimestamp(stamps[0] / 1e9, tz=timezone.utc)
    return MouseTrace(
        filename=Path(path).name,
        timestamps_ns=stamps,
        elapsed_s=elapsed,
        temperatures_c=df.temperature_c.to_numpy(),
        start_utc=start,
        mouse_id=str(df['mouse_id'].iloc[0]),
    )


def one_second_grid(seconds: np.ndarray, temperatures: np.ndarray, duration: int) -> pd.DataFrame:
    """Select one actual observation per [s, s+1) bin nearest its midpoint.

    Empty bins stay NaN. Preserves raw row order and distances without temperature fitting.
    """
    seconds = np.asarray(seconds, dtype=float)
    temperatures = np.asarray(temperatures, dtype=float)
    if seconds.shape != temperatures.shape or not np.isfinite(seconds).all():
        raise ValueError('Invalid timestamps or mismatched arrays')
    if ((seconds < 0) | (seconds >= duration)).any():
        raise ValueError('Source timestamp outside selected interval')

    slot = np.floor(seconds).astype(np.int64)
    raw = pd.DataFrame(dict(
        second=slot,
        source_second=seconds,
        temperature_c=temperatures,
        source_row=np.arange(1, len(seconds) + 1),
    ))
    raw['bin_center_offset_s'] = seconds - (slot + 0.5)
    raw['distance'] = raw.bin_center_offset_s.abs()
    chosen = raw.sort_values(['second', 'distance', 'source_row']).drop_duplicates('second')
    grid = chosen.drop(columns='distance').set_index('second').reindex(range(duration))
    grid.index.name = 'second'
    grid['source_row'] = grid.source_row.astype('Int64')
    return grid


def align_mouse_data(
    df: pd.DataFrame,
    start_time: str | dt_time,
    window_hours: float,
    timezone_name: str,
) -> tuple[np.ndarray, np.ndarray, pd.Timestamp]:
    """Align mouse timestamps so Time 0 starts at the specified start clock time.

    If a continuous window of `window_hours` starting at `start_time` is present,
    it is extracted. If the recording is shorter or started mid-day, circadian phase
    folding modulo 24h is applied relative to `start_time`.
    """
    zone = ZoneInfo(timezone_name)
    target_time = parse_time_str(start_time)
    target_sec = target_time.hour * 3600 + target_time.minute * 60 + target_time.second
    duration_s = int(round(window_hours * 3600))

    times_local = df.datetime_utc.dt.tz_convert(zone)
    earliest = times_local.iloc[0]
    latest = times_local.iloc[-1]

    # Look for candidate calendar days where target_time falls within recording
    candidates = []
    current_date = earliest.date()
    while current_date <= latest.date():
        cand = pd.Timestamp(
            year=current_date.year, month=current_date.month, day=current_date.day,
            hour=target_time.hour, minute=target_time.minute, second=target_time.second,
            tz=zone
        )
        if cand >= earliest and cand + pd.Timedelta(seconds=duration_s) <= latest + pd.Timedelta(seconds=120):
            candidates.append(cand)
        current_date += timedelta(days=1)

    if candidates:
        # Continuous window from first valid start
        start_ts = candidates[0]
        end_ts = start_ts + pd.Timedelta(seconds=duration_s)
        selected = (times_local >= start_ts) & (times_local < end_ts)
        sec = (times_local[selected] - start_ts).dt.total_seconds().to_numpy()
        temp = df.loc[selected, 'temperature_c'].to_numpy()
        return sec, temp, start_ts

    # Fallback to circadian phase folding relative to target start time
    start_ts = pd.Timestamp(
        year=earliest.year, month=earliest.month, day=earliest.day,
        hour=target_time.hour, minute=target_time.minute, second=target_time.second,
        tz=zone
    )
    if start_ts > earliest:
        start_ts -= pd.Timedelta(days=1)

    # Fold modulo window duration
    local_sec = (times_local.dt.hour * 3600 + times_local.dt.minute * 60 + times_local.dt.second +
                 times_local.dt.microsecond / 1e6 + times_local.dt.nanosecond / 1e9).to_numpy()
    days_elapsed = (times_local - earliest).dt.total_seconds().to_numpy() // 86400
    sec = (days_elapsed * 86400 + (local_sec - target_sec) % 86400)
    selected = (sec >= 0) & (sec < duration_s)
    return sec[selected], df.loc[selected, 'temperature_c'].to_numpy(), start_ts


def aggregate_grid(grid: pd.DataFrame, mask: np.ndarray | None = None, bin_minutes: int = 30) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Calculate 1-minute maxima and 30-minute means from a 1-second grid."""
    use = grid.temperature_c.notna().to_numpy()
    if mask is not None:
        if len(grid) % len(mask):
            raise ValueError('Coverage mask must tile grid length')
        use = use & np.tile(mask, len(grid) // len(mask))

    total_seconds = len(grid)
    total_minutes = total_seconds // 60
    minute_ids = grid.index.to_numpy() // 60
    temps = grid.temperature_c.to_numpy()

    minute_rows = []
    for m in range(total_minutes):
        in_min = (minute_ids == m) & use
        valid_samples = temps[in_min & np.isfinite(temps)]
        count = len(valid_samples)
        m_max = float(np.max(valid_samples)) if count >= 30 else float('nan')
        minute_rows.append(dict(minute=m, samples=count, temperature_c=m_max, usable=count >= 30))
    minutes_df = pd.DataFrame(minute_rows)

    bins_count = total_minutes // bin_minutes
    bin_rows = []
    min_usable_in_bin = int(np.ceil(bin_minutes * 0.8))
    for b in range(bins_count):
        sub = minutes_df.iloc[b * bin_minutes:(b + 1) * bin_minutes]
        usable = sub[sub.usable & sub.temperature_c.notna()]
        mean_val = float(usable.temperature_c.mean()) if len(usable) >= min_usable_in_bin else float('nan')
        bin_rows.append(dict(bin=b, temperature_c=mean_val, valid_minutes=len(usable)))
    bins_df = pd.DataFrame(bin_rows)
    return minutes_df, bins_df


def _validate_start_phases(traces_by_group: dict, config=DEFAULT_PLOT_SETTINGS):
    """Backwards-compatibility check for tests verifying start-time phase similarity."""
    all_traces = [(g, t) for g, traces in traces_by_group.items() for t in traces]
    if len(all_traces) < 2:
        return
    zone = ZoneInfo(config.get('TIMEZONE', 'Asia/Jerusalem'))
    ref_g, ref_t = all_traces[0]
    ref_dt = ref_t.start_utc.astimezone(zone)
    ref_m = ref_dt.hour * 60 + ref_dt.minute + ref_dt.second / 60
    tol = float(config.get('MAX_START_TIME_DIFF_MINUTES', 60))
    for g, t in all_traces[1:]:
        cur_dt = t.start_utc.astimezone(zone)
        cur_m = cur_dt.hour * 60 + cur_dt.minute + cur_dt.second / 60
        diff = abs((cur_m - ref_m + 720) % 1440 - 720)
        if diff > tol:
            raise ValueError(
                f'Start-time mismatch: {ref_t.filename} in {ref_g} vs {t.filename} in {g}; '
                f'local clock phases differ by {diff:.2f} min (limit {tol:g} min).'
            )


def _mouse_profile(trace: MouseTrace, config=DEFAULT_PLOT_SETTINGS) -> dict:
    """Compute normalized profile and phase statistics for a single mouse trace."""
    start_time = config.get('START_TIME', f"{config.get('LIGHTS_ON_HOUR', 7):02d}:00")
    window_hours = float(config.get('TIME_WINDOW_HOURS', config.get('PLOT_HOURS', 24)))
    bin_minutes = int(config.get('BIN_MINUTES', 30))
    timezone_name = config.get('TIMEZONE', 'Asia/Jerusalem')
    duration_s = int(round(window_hours * 3600))

    # Grid the trace elapsed seconds
    grid = one_second_grid(
        np.clip(trace.elapsed_s[:duration_s], 0, duration_s - 1),
        trace.temperatures_c[:duration_s],
        duration=duration_s
    )
    minutes, bins = aggregate_grid(grid, bin_minutes=bin_minutes)
    profile = bins.temperature_c.to_numpy()
    baseline = float(np.nanmean(profile))
    profile_delta = profile - baseline

    centers_h = (np.arange(len(bins)) + 0.5) * (bin_minutes / 60)
    zone = ZoneInfo(timezone_name)

    on_time = parse_time_str(config.get('LIGHTS_ON_TIME', f"{config.get('LIGHTS_ON_HOUR', 7):02d}:00"))
    off_time = parse_time_str(config.get('LIGHTS_OFF_TIME', f"{config.get('LIGHTS_OFF_HOUR', 19):02d}:00"))
    on_m = on_time.hour * 60 + on_time.minute
    off_m = off_time.hour * 60 + off_time.minute

    is_light = np.zeros(len(bins), dtype=bool)
    for i, h in enumerate(centers_h):
        stamp = datetime.fromtimestamp(trace.start_utc.timestamp() + h * 3600, timezone.utc).astimezone(zone)
        local_minute = stamp.hour * 60 + stamp.minute + stamp.second / 60
        if on_m < off_m:
            is_light[i] = (on_m <= local_minute < off_m)
        else:
            is_light[i] = (local_minute >= on_m or local_minute < off_m)

    light_vals = profile_delta[is_light & np.isfinite(profile_delta)]
    dark_vals = profile_delta[(~is_light) & np.isfinite(profile_delta)]
    light_mean = float(light_vals.mean()) if light_vals.size else float('nan')
    dark_mean = float(dark_vals.mean()) if dark_vals.size else float('nan')

    return dict(
        trace=trace,
        x=centers_h,
        profile=profile_delta,
        baseline_c=baseline,
        light_mean=light_mean,
        dark_mean=dark_mean,
        dark_minus_light=dark_mean - light_mean,
        is_light=is_light,
    )


def compute_group_analysis(
    input_dir: str | Path | None,
    groups: dict[str, list[str | Path]],
    settings: dict | None = None,
) -> dict:
    """Execute complete multi-mouse group analysis and return tables and profiles."""
    cfg = dict(DEFAULT_PLOT_SETTINGS)
    if settings:
        cfg.update(settings)

    root = Path(input_dir).expanduser() if input_dir else Path.cwd()
    start_time = cfg.get('START_TIME', f"{cfg.get('LIGHTS_ON_HOUR', 7):02d}:00")
    window_hours = float(cfg.get('TIME_WINDOW_HOURS', cfg.get('PLOT_HOURS', 24)))
    bin_minutes = int(cfg.get('BIN_MINUTES', 30))
    duration_s = int(round(window_hours * 3600))
    timezone_name = cfg.get('TIMEZONE', 'Asia/Jerusalem')
    match_common = bool(cfg.get('MATCH_COMMON_SUPPORT', True))

    on_time = parse_time_str(cfg.get('LIGHTS_ON_TIME', f"{cfg.get('LIGHTS_ON_HOUR', 7):02d}:00"))
    off_time = parse_time_str(cfg.get('LIGHTS_OFF_TIME', f"{cfg.get('LIGHTS_OFF_HOUR', 19):02d}:00"))
    on_sec = on_time.hour * 3600 + on_time.minute * 60
    off_sec = off_time.hour * 3600 + off_time.minute * 60
    start_sec = parse_time_str(start_time).hour * 3600 + parse_time_str(start_time).minute * 60

    # Load and align each mouse
    raw_data = {}
    grids = {}
    for group_name, file_list in groups.items():
        if not file_list:
            continue
        raw_data[group_name] = []
        for file_item in file_list:
            path = Path(file_item)
            if not path.is_absolute():
                path = root / path
            df = load_mouse_csv(path, cfg)
            sec, temp, start_ts = align_mouse_data(df, start_time, window_hours, timezone_name)
            grid = one_second_grid(sec, temp, duration=duration_s)
            mouse_id = str(df['mouse_id'].iloc[0])
            grids[mouse_id] = grid
            raw_data[group_name].append({
                'mouse_id': mouse_id,
                'path': path,
                'grid': grid,
                'start_ts': start_ts,
            })

    if not grids:
        raise ValueError('No valid mouse data found to analyze.')

    # Compute common support mask if requested
    mask = None
    if match_common and len(grids) > 1:
        mask = np.logical_and.reduce([g.temperature_c.notna().to_numpy() for g in grids.values()])
        if mask.sum() < 0.2 * duration_s:
            warnings.warn(
                f'Strict common sampling support retained only {mask.sum():,} / {duration_s:,} seconds. '
                'Falling back to independent sampling.', RuntimeWarning, stacklevel=2
            )
            mask = None

    # Aggregate into bins and profiles
    bin_count = duration_s // (bin_minutes * 60)
    centers_h = (np.arange(bin_count) + 0.5) * (bin_minutes / 60)

    # Determine light/dark phase for each bin
    is_light = np.zeros(bin_count, dtype=bool)
    for i, h in enumerate(centers_h):
        clock_sec = (start_sec + int(round(h * 3600))) % 86400
        if on_sec < off_sec:
            is_light[i] = (on_sec <= clock_sec < off_sec)
        else:
            is_light[i] = (clock_sec >= on_sec or clock_sec < off_sec)

    profiles_list = []
    phase_rows = []
    paired_rows = []

    for group_name, mice in raw_data.items():
        for m in mice:
            minutes, bins = aggregate_grid(m['grid'], mask, bin_minutes=bin_minutes)
            raw_prof = bins.temperature_c.to_numpy()
            baseline = float(np.nanmean(raw_prof))
            delta = raw_prof - baseline

            l_vals = delta[is_light & np.isfinite(delta)]
            d_vals = delta[(~is_light) & np.isfinite(delta)]
            l_mean = float(l_vals.mean()) if l_vals.size else float('nan')
            d_mean = float(d_vals.mean()) if d_vals.size else float('nan')
            diff = d_mean - l_mean

            paired_rows.append(dict(
                genotype=group_name,
                animal=m['mouse_id'],
                light=l_mean,
                dark=d_mean,
                decrease=diff,
                baseline_c=baseline,
            ))
            phase_rows.append(dict(genotype=group_name, animal=m['mouse_id'], phase='light', temperature_change_c=l_mean))
            phase_rows.append(dict(genotype=group_name, animal=m['mouse_id'], phase='dark', temperature_change_c=d_mean))

            for i in range(len(bins)):
                profiles_list.append(dict(
                    genotype=group_name,
                    animal=m['mouse_id'],
                    zt_hour=centers_h[i],
                    elapsed_hour=centers_h[i],
                    temperature_change_c=delta[i],
                    absolute_temperature_c=raw_prof[i],
                    phase='light' if is_light[i] else 'dark',
                ))

    return dict(
        data=pd.DataFrame(profiles_list),
        phase=pd.DataFrame(phase_rows),
        paired=pd.DataFrame(paired_rows).set_index(['genotype', 'animal']),
        window_hours=window_hours,
        start_sec=start_sec,
        on_sec=on_sec,
        off_sec=off_sec,
        mask=mask,
        config=cfg,
    )


def draw_group_figure(analysis_results: dict) -> plt.Figure:
    """Render publication-quality 3-panel summary figure."""
    data = analysis_results['data']
    phase = analysis_results['phase']
    paired = analysis_results['paired']
    window_hours = analysis_results['window_hours']
    start_sec = analysis_results['start_sec']
    on_sec = analysis_results['on_sec']
    off_sec = analysis_results['off_sec']

    genotypes = list(dict.fromkeys(data.genotype))
    group_colors = {}
    group_styles = {}
    for i, g in enumerate(genotypes):
        if g.lower() == 'wt':
            group_colors[g] = WT_COLOR
        elif 'dravet' in g.lower():
            group_colors[g] = DRAVET_COLOR
        else:
            group_colors[g] = PALETTE[i % len(PALETTE)]
        group_styles[g] = LINE_STYLES[i % len(LINE_STYLES)]

    plt.rcParams.update({
        'font.family': 'DejaVu Sans',
        'font.size': 9,
        'axes.titlesize': 12,
        'axes.labelsize': 10,
        'figure.facecolor': 'white',
        'savefig.facecolor': 'white',
    })

    fig_w = max(10.65, 10.65 * (window_hours / 24.0) ** 0.3)
    fig_h = 3.9
    fig = plt.figure(figsize=(fig_w, fig_h))

    # Panel geometry
    ax_contrast = fig.add_axes([0.94 / fig_w, 0.66 / fig_h, 2.35 / fig_w, 2.24 / fig_h])
    prof_w = fig_w - 5.85
    ax_profile = fig.add_axes([3.70 / fig_w, 0.66 / fig_h, prof_w / fig_w, 2.24 / fig_h])
    ax_summary = fig.add_axes([(fig_w - 2.85) / fig_w, 0.66 / fig_h, 2.35 / fig_w, 2.24 / fig_h])

    for ax in (ax_contrast, ax_profile, ax_summary):
        ax.spines[['top', 'right']].set_visible(False)
        ax.spines[['left', 'bottom']].set_color(BORDER)
        ax.grid(False)
        ax.tick_params(length=3.5, width=0.8, direction='out', pad=6, labelsize=9, colors=INK)

    # ------------------ PANEL 1: Light-Dark Contrast ------------------
    ax_contrast.set_title('Light–dark temperature\ncontrast', pad=15, weight='bold', color=INK)
    ax_contrast.axvspan(-0.35, 0.5, color=LIGHT_BG, linewidth=0, zorder=0)
    ax_contrast.axvspan(0.5, 1.35, color=DARK_BG, linewidth=0, zorder=0)

    for g in genotypes:
        color = group_colors[g]
        style = group_styles[g]
        group_p = paired.loc[g]
        # Individual mice
        for _, row in group_p.iterrows():
            ax_contrast.plot([0, 1], [row.light, row.dark], color=color, alpha=0.45,
                             linewidth=1, marker='o', markersize=4, markerfacecolor='white')
        # Group mean
        mean_l = group_p.light.mean()
        mean_d = group_p.dark.mean()
        ax_contrast.plot([0, 1], [mean_l, mean_d], color=color, linewidth=2.1,
                         linestyle=style, marker='o', markersize=6, markeredgecolor='white', zorder=5)

        for x_pos, val in enumerate([mean_l, mean_d]):
            offset = 10 if val >= 0 else -18
            ax_contrast.annotate(f'{val:+.2f}°C', (x_pos, val), xytext=(0, offset),
                                textcoords='offset points', ha='center', color=color,
                                fontsize=9, weight='bold')

    max_val = max(0.5, abs(paired[['light', 'dark']].to_numpy()).max() + 0.25)
    ax_contrast.set_ylim(-max_val, max_val)
    ax_contrast.set_xlim(-0.35, 1.35)
    ax_contrast.set_xticks([0, 1], ['LIGHT', 'DARK'])
    ax_contrast.set_ylabel('Mean Δ temperature (°C)', color=INK)

    # ------------------ PANEL 2: Temperature Profile ------------------
    ax_profile.set_title('Temperature profile', pad=31, weight='bold', color=INK)
    ax_profile.set_xlim(0, window_hours)

    # Dynamic light/dark bands
    # Identify phase transitions
    step_m = 5
    grid_points = int(window_hours * 60 / step_m)
    phase_flags = []
    for step in range(grid_points):
        h = (step + 0.5) * (step_m / 60.0)
        c_sec = (start_sec + int(round(h * 3600))) % 86400
        if on_sec < off_sec:
            is_l = (on_sec <= c_sec < off_sec)
        else:
            is_l = (c_sec >= on_sec or c_sec < off_sec)
        phase_flags.append(is_l)

    # Find continuous blocks
    blocks = []
    curr_l = phase_flags[0]
    b_start = 0.0
    for idx, f in enumerate(phase_flags):
        if f != curr_l:
            blocks.append((b_start, idx * (step_m / 60.0), curr_l))
            b_start = idx * (step_m / 60.0)
            curr_l = f
    blocks.append((b_start, window_hours, curr_l))

    for b_s, b_e, is_l in blocks:
        ax_profile.axvspan(b_s, b_e, color=LIGHT_BG if is_l else DARK_BG, linewidth=0, zorder=0)
        if b_s > 0:
            ax_profile.axvline(b_s, color=DIVIDER, linewidth=0.8)
        mid = (b_s + b_e) / 2.0
        label_text = 'LIGHT' if is_l else 'DARK'
        ax_profile.text(mid / window_hours, 1.025, label_text, transform=ax_profile.transAxes,
                        ha='center', va='bottom', fontsize=9, color=INK, weight='bold')

    # Traces and group lines
    all_trace_min, all_trace_max = -0.5, 0.5
    for g in genotypes:
        color = group_colors[g]
        style = group_styles[g]
        g_data = data[data.genotype == g]
        piv = g_data.pivot(index='zt_hour', columns='animal', values='temperature_change_c').sort_index()
        # Extend first and last bin values to boundaries
        piv = pd.concat([piv.iloc[[0]].set_axis([0.0]), piv, piv.iloc[[-1]].set_axis([float(window_hours)])])
        mean_s = piv.mean(axis=1)

        if piv.shape[1] > 1:
            sd_s = piv.std(axis=1, ddof=1)
            ax_profile.fill_between(piv.index, mean_s - sd_s, mean_s + sd_s, color=color, alpha=0.14, linewidth=0)
            for animal in piv.columns:
                ax_profile.plot(piv.index, piv[animal], color=color, linewidth=0.65, alpha=0.25)
        ax_profile.plot(piv.index, mean_s, color=color, linewidth=1.9, linestyle=style, zorder=4)

        all_trace_min = min(all_trace_min, float(piv.min().min()))
        all_trace_max = max(all_trace_max, float(piv.max().max()))

    y_low = np.floor(min(all_trace_min, -1.0) * 2) / 2
    y_high = np.ceil(max(all_trace_max, 1.0) * 2) / 2
    ax_profile.set_ylim(y_low, y_high)
    ax_profile.set_yticks(np.arange(y_low, y_high + 0.1, 0.5 if (y_high - y_low) <= 4 else 1.0))
    ax_profile.set_ylabel('Δ temperature (°C)', color=INK)
    ax_profile.set_xlabel('Time (h)', color=INK, labelpad=7)

    tick_step = 6 if window_hours <= 24 else 12
    ax_profile.set_xticks(np.arange(0, window_hours + 0.1, tick_step))

    # ------------------ PANEL 3: Temperature Decrease ------------------
    ax_summary.set_title('Temperature decrease\nduring light', pad=15, weight='bold', color=INK)
    upper_lim, lower_lim = 0.0, -0.1

    for x_idx, g in enumerate(genotypes):
        color = group_colors[g]
        values = paired.loc[g, 'decrease']
        vals_arr = np.asarray(values, dtype=float)
        mean_val = float(vals_arr.mean())
        jitter = np.linspace(-0.12, 0.12, len(vals_arr)) if len(vals_arr) > 1 else np.array([0.0])

        ax_summary.scatter(x_idx + jitter, vals_arr, s=31, facecolor='white',
                           edgecolor=color, linewidth=1.3, zorder=5)
        top = float(vals_arr.max())
        lower_lim = min(lower_lim, float(vals_arr.min()) - 0.15)

        if len(vals_arr) > 1:
            sd_val = float(vals_arr.std(ddof=1))
            ax_summary.errorbar(x_idx, mean_val, yerr=sd_val, color=color,
                                capsize=4, linewidth=1.7, zorder=4)
            top = max(top, mean_val + sd_val)
            lower_lim = min(lower_lim, mean_val - sd_val - 0.15)

        ax_summary.scatter([x_idx], [mean_val], s=58, marker='D', color=color,
                           edgecolor='white', zorder=6)
        ax_summary.text(x_idx, top + 0.1, f'{mean_val:.2f}°C', ha='center', va='bottom',
                        color=color, fontsize=11, weight='bold')
        upper_lim = max(upper_lim, top + 0.3)

    ax_summary.set_xticks(range(len(genotypes)), genotypes)
    ax_summary.set_xlim(-0.5, len(genotypes) - 0.5)
    ax_summary.set_ylim(lower_lim, upper_lim)
    ax_summary.set_ylabel('Dark − light (°C)', color=INK)

    # ------------------ TOP LEGEND ------------------
    counts = data.groupby('genotype').animal.nunique()
    handles = [
        Line2D([0], [0], color=group_colors[g], linewidth=2,
               linestyle=group_styles[g], label=f"{g} (n={counts[g]})")
        for g in genotypes
    ]
    fig.legend(handles=handles, loc='upper center', bbox_to_anchor=(0.5, 0.985),
               ncol=min(len(handles), 4), frameon=False, handlelength=3,
               columnspacing=3, labelcolor='linecolor', fontsize=10)

    return fig


def create_group_plot(input_dir: str | Path | None, groups: dict[str, list[str | Path]], *, settings=None) -> Path:
    """End-to-end API: runs analysis, saves PNG figure only, and writes summary CSV files."""
    results = compute_group_analysis(input_dir, groups, settings=settings)
    cfg = results['config']
    fig = draw_group_figure(results)

    out_dir = Path(cfg['OUTPUT_DIR']).expanduser() if cfg.get('OUTPUT_DIR') else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = cfg.get('OUTPUT_FILENAME', 'multi_mouse_thermal_summary.png')
    if not filename.lower().endswith('.png'):
        filename += '.png'
    out_png = out_dir / filename

    fig.savefig(out_png, dpi=int(cfg.get('DPI', 300)))
    plt.close(fig)

    # Save summary CSV tables next to the PNG for traceability
    results['paired'].to_csv(out_dir / 'per_mouse_dark_minus_light.csv')
    results['phase'].to_csv(out_dir / 'per_mouse_phase_summary.csv', index=False)
    results['data'].to_csv(out_dir / 'extracted_temperature_timeseries.csv', index=False)

    return out_png
