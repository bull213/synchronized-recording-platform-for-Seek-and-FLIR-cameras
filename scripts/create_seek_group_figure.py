#!/usr/bin/env python3
"""Create a 24-hour temperature summary from native Seek thermal CSV files.

Each CSV contains full-frame camera pixels for one mouse. Group labels and
mouse IDs can be supplied on the command line; the figure supports arbitrary
sample and group counts.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np


LIGHT_BG = '#FFF3D7'
DARK_BG = '#E8EEF6'
INK = '#26344A'
SECONDS_PER_DAY = 86_400
NS = 1_000_000_000
FRAME_FIELDS = re.compile(rb'(?:^|[,|])\s*([A-Za-z0-9_]+)\s*=\s*([^,|\r\n]+)')


def frame_header(line, path, frame_number):
    fields = {match.group(1).decode('ascii').lower(): match.group(2).decode('ascii').strip()
              for match in FRAME_FIELDS.finditer(line)}
    try:
        width, height, stamp = int(fields['width']), int(fields['height']), int(fields['timestamp_utc_ns'])
    except (KeyError, ValueError) as exc:
        raise ValueError(f'{path}: malformed Seek frame header near frame {frame_number}.') from exc
    if not (0 < width <= 4096 and 0 < height <= 4096 and stamp > 0):
        raise ValueError(f'{path}: invalid frame size or timestamp near frame {frame_number}.')
    return width, height, stamp


def first_timestamp(path):
    with path.open('rb', buffering=1024 * 1024) as source:
        while line := source.readline():
            if b'width=' in line and b'height=' in line:
                return frame_header(line, path, 1)[2]
    raise ValueError(f'{path}: no Seek frame headers found.')


def frame_roi_mean(crop):
    """Mean finite temperatures in the clipped 7x7 patch around the ROI max."""
    pixels = np.asarray(crop, dtype=np.float64)
    finite = np.isfinite(pixels)
    if not finite.any():
        return np.nan
    max_index = int(np.argmax(np.where(finite, pixels, -np.inf)))
    y, x = np.unravel_index(max_index, pixels.shape)
    patch = pixels[max(0, y - 3):min(pixels.shape[0], y + 4),
                   max(0, x - 3):min(pixels.shape[1], x + 4)]
    patch = patch[np.isfinite(patch)]
    return float(patch.mean()) if patch.size else np.nan


def iter_one_hz_roi_means(item, start_ns, progress=True):
    """Select the frame nearest each second midpoint from native Seek pixels."""
    current_second = best_delta = best_value = None
    previous_ns = None
    frames = 0

    def publish(second, value):
        if second is not None and value is not None and 0 <= second < SECONDS_PER_DAY:
            return second, value
        return None

    with item['path'].open('rb', buffering=8 * 1024 * 1024) as source:
        while header := source.readline():
            if b'width=' not in header or b'height=' not in header:
                continue
            width, height, stamp = frame_header(header, item['path'], frames + 1)
            if previous_ns is not None and stamp <= previous_ns:
                raise ValueError(f'{item["mouse_id"]}: Seek timestamps must increase strictly.')
            previous_ns = stamp
            frames += 1
            if stamp >= start_ns + SECONDS_PER_DAY * NS:
                break
            second = (stamp - start_ns) // NS
            if current_second is not None and second != current_second:
                value = publish(int(current_second), best_value)
                if value is not None:
                    yield value
                current_second, best_delta, best_value = None, None, None
            should_parse = False
            if second >= 0:
                delta = abs((stamp - start_ns) - (second * NS + NS // 2))
                should_parse = current_second is None or delta < best_delta
            if should_parse:
                rows = []
                for row_number in range(height):
                    row = source.readline()
                    if not row or (b'width=' in row and b'height=' in row):
                        raise ValueError(f'{item["path"]}: truncated frame {frames} at pixel row {row_number + 1}.')
                    values = np.fromstring(row.strip().removesuffix(b','), sep=',', dtype=np.float64)
                    if values.size != width:
                        raise ValueError(f'{item["path"]}: malformed pixel row {row_number + 1} in frame {frames}.')
                    rows.append(values)
                best_value = frame_roi_mean(np.stack(rows))
                current_second, best_delta = int(second), delta
            else:
                for _ in range(height):
                    if not source.readline():
                        raise ValueError(f'{item["path"]}: truncated frame {frames}.')
            if progress and frames % 100_000 == 0:
                print(f'  {item["mouse_id"]}: scanned {frames:,} frames...', file=sys.stderr, flush=True)
    value = publish(current_second, best_value)
    if value is not None:
        yield value
    if frames == 0:
        raise ValueError(f'{item["mouse_id"]}: no pixel frames found.')
    if previous_ns is None or previous_ns < start_ns + (SECONDS_PER_DAY - 1) * NS:
        raise ValueError(f'{item["mouse_id"]}: recording does not cover the complete selected 24-hour window.')
    print(f'  {item["mouse_id"]}: scanned {frames:,} frames.', file=sys.stderr)


def parse_clock(text):
    try:
        hour, minute = map(int, text.split(':'))
    except (ValueError, AttributeError) as exc:
        raise ValueError('Clock times must use HH:MM.') from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError('Clock times must use HH:MM in the 24-hour clock.')
    return hour, minute


def parse_start(text, timezone_name):
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError('--window-start must be YYYY-MM-DDTHH:MM[:SS] local time.') from exc
    if parsed.tzinfo is not None:
        raise ValueError('--window-start must be local time without a UTC offset.')
    return parsed.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)


def reduce_profile(samples, item):
    seconds = np.full(SECONDS_PER_DAY, np.nan)
    for second, value in samples:
        seconds[second] = value
    minutes = seconds.reshape(1440, 60)
    counts = np.isfinite(minutes).sum(axis=1)
    minute_max = np.full(1440, np.nan)
    valid = counts >= 30
    if valid.any():
        minute_max[valid] = np.nanmax(minutes[valid], axis=1)
    blocks = minute_max.reshape(48, 30)
    valid_minutes = np.isfinite(blocks).sum(axis=1)
    half_hour = np.full(48, np.nan)
    valid_blocks = valid_minutes >= 24
    if valid_blocks.any():
        half_hour[valid_blocks] = np.nanmean(blocks[valid_blocks], axis=1)
    if not np.isfinite(half_hour).any():
        raise ValueError(f'{item["mouse_id"]}: no half-hour bins met the coverage threshold.')
    baseline = float(np.nanmean(half_hour))
    return {'genotype': item['genotype'], 'absolute': half_hour,
            'change': half_hour - baseline, 'baseline': baseline,
            'valid_bins': int(np.isfinite(half_hour).sum())}


def draw_figure(per_mouse, start_utc, timezone_name, lights_on, lights_off, out):
    tz = ZoneInfo(timezone_name)
    lo, lf = parse_clock(lights_on), parse_clock(lights_off)
    if lo == lf:
        raise ValueError('Lights-on and lights-off times cannot be equal.')
    genotypes = list(dict.fromkeys(row['genotype'] for row in per_mouse.values()))
    colors = plt.get_cmap('tab10')
    group_colors = {group: colors(i % 10) for i, group in enumerate(genotypes)}
    ids_by_group = {group: [name for name, row in per_mouse.items() if row['genotype'] == group]
                    for group in genotypes}
    end_utc = start_utc + timedelta(hours=24)
    boundaries = [start_utc]
    origin = start_utc.astimezone(tz)
    for day_shift in range(-1, 3):
        date = origin.date() + timedelta(days=day_shift)
        for hour, minute in (lo, lf):
            transition = datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)
            if start_utc < transition < end_utc:
                boundaries.append(transition)
    boundaries.append(end_utc)
    boundaries.sort()
    light_hours, dark_hours = [], []
    lo_minute, lf_minute = lo[0] * 60 + lo[1], lf[0] * 60 + lf[1]
    for left, right in zip(boundaries, boundaries[1:]):
        mid = (left + (right - left) / 2).astimezone(tz)
        minute = mid.hour * 60 + mid.minute
        is_light = lo_minute <= minute < lf_minute if lo_minute < lf_minute else minute >= lo_minute or minute < lf_minute
        span = ((left - start_utc).total_seconds() / 3600, (right - start_utc).total_seconds() / 3600)
        (light_hours if is_light else dark_hours).append(span)
    hours = (np.arange(48) + .5) / 2
    phase_labels = []
    for hour in hours:
        local = (start_utc + timedelta(hours=float(hour))).astimezone(tz)
        minute = local.hour * 60 + local.minute
        phase_labels.append('light' if (lo_minute <= minute < lf_minute if lo_minute < lf_minute
                                         else minute >= lo_minute or minute < lf_minute) else 'dark')
    phase_means, paired = {}, {}
    for group in genotypes:
        phase_values = {'light': [], 'dark': []}
        for name in ids_by_group[group]:
            row = per_mouse[name]['change']
            for phase in ('light', 'dark'):
                values = row[np.array(phase_labels) == phase]
                phase_values[phase].append(float(np.nanmean(values)) if np.isfinite(values).any() else np.nan)
        phase_means[group] = {phase: float(np.nanmean(vals)) for phase, vals in phase_values.items()}
        paired[group] = np.array(phase_values['dark']) - np.array(phase_values['light'])
    if any(not np.isfinite(v) for group in phase_means.values() for v in group.values()):
        raise ValueError('Each mouse needs usable half-hour data in both light and dark periods.')

    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9, 'axes.titlesize': 12,
                         'axes.labelsize': 10, 'svg.fonttype': 'none', 'pdf.fonttype': 42,
                         'figure.facecolor': 'white', 'savefig.facecolor': 'white'})
    fig, axes = plt.subplots(1, 3, figsize=(12.2, 4.1), gridspec_kw={'width_ratios': [1, 1.1, 1]})
    fig.subplots_adjust(left=.07, right=.98, bottom=.19, top=.78, wspace=.42)
    for ax in axes:
        ax.spines[['top', 'right']].set_visible(False)
        ax.spines[['left', 'bottom']].set_color('#D2DAE4')
        ax.tick_params(length=3.5, width=.8, direction='out', pad=6, labelsize=9, colors=INK)
    ax = axes[0]
    ax.set_title('Light–dark temperature\ncontrast', pad=12, weight='bold', color=INK)
    ax.axvspan(-.35, .5, color=LIGHT_BG, lw=0, zorder=0)
    ax.axvspan(.5, 1.35, color=DARK_BG, lw=0, zorder=0)
    xpositions = np.arange(len(genotypes))
    for group in genotypes:
        color = group_colors[group]
        for name in ids_by_group[group]:
            row = per_mouse[name]['change']
            means = [float(np.nanmean(row[np.array(phase_labels) == p])) for p in ('light', 'dark')]
            ax.plot([0, 1], means, color=color, alpha=.3, lw=.8, marker='o', ms=3)
        means = phase_means[group]
        ax.plot([0, 1], [means['light'], means['dark']], color=color, lw=2,
                marker='o', ms=5, markeredgecolor='white', zorder=5)
    ax.set_xlim(-.35, 1.35)
    ax.set_xticks([0, 1], ['LIGHT', 'DARK'])
    ax.set_ylabel('Mean Δ temperature (°C)', color=INK)

    ax = axes[1]
    ax.set_title('Temperature profile', pad=12, weight='bold', color=INK)
    for a, b in light_hours: ax.axvspan(a, b, color=LIGHT_BG, lw=0, zorder=0)
    for a, b in dark_hours: ax.axvspan(a, b, color=DARK_BG, lw=0, zorder=0)
    for transition in boundaries[1:-1]:
        ax.axvline((transition - start_utc).total_seconds() / 3600, color='#A5B2C4', lw=.8, zorder=1)
    all_values = []
    for group in genotypes:
        matrix = np.vstack([per_mouse[name]['change'] for name in ids_by_group[group]])
        mean = np.nanmean(matrix, axis=0)
        color = group_colors[group]
        if len(matrix) > 1:
            sd = np.nanstd(matrix, axis=0, ddof=1)
            ax.fill_between(hours, mean - sd, mean + sd, color=color, alpha=.12, lw=0)
        for name in ids_by_group[group]:
            ax.plot(hours, per_mouse[name]['change'], color=color, lw=.55, alpha=.25)
        ax.plot(hours, mean, color=color, lw=1.8, zorder=4)
        all_values.extend(matrix[np.isfinite(matrix)].tolist())
    low, high = min(-1.5, np.floor(min(all_values) * 2) / 2), max(1.5, np.ceil(max(all_values) * 2) / 2)
    ax.set_ylim(low, high); ax.set_xlim(0, 24); ax.set_xticks([0, 12, 24])
    ax.set_ylabel('Δ temperature (°C)', color=INK); ax.set_xlabel('Time (h)', color=INK, labelpad=7)
    for a, b in light_hours + dark_hours:
        if b - a >= 2:
            is_light = (a, b) in light_hours
            ax.text((a + b) / 48, 1.02, 'LIGHT' if is_light else 'DARK', transform=ax.transAxes,
                    ha='center', va='bottom', fontsize=8, color=INK, weight='bold')

    ax = axes[2]
    ax.set_title('Temperature decrease\nduring light', pad=12, weight='bold', color=INK)
    for x, group in zip(xpositions, genotypes):
        values = paired[group]
        color = group_colors[group]
        jitter = np.linspace(-.12, .12, len(values)) if len(values) > 1 else np.array([0.])
        ax.scatter(x + jitter, values, s=28, facecolor='white', edgecolor=color, lw=1.2, zorder=5)
        mean = float(np.nanmean(values)); low_g = float(np.nanmin(values)); high_g = float(np.nanmax(values))
        if len(values) > 1:
            sd = float(np.nanstd(values, ddof=1))
            ax.errorbar(x, mean, yerr=sd, color=color, capsize=3, lw=1.4, zorder=4)
            low_g, high_g = min(low_g, mean - sd), max(high_g, mean + sd)
        ax.scatter([x], [mean], s=48, marker='D', color=color, edgecolor='white', zorder=6)
        ax.text(x, high_g + .08, f'{mean:.2f}', ha='center', va='bottom', color=color, fontsize=9, weight='bold')
    group_values = np.concatenate(list(paired.values()))
    ax.set_ylim(min(-.1, float(np.nanmin(group_values)) - .25), max(.3, float(np.nanmax(group_values)) + .4))
    ax.set_xticks(xpositions, genotypes, rotation=30 if len(genotypes) > 3 else 0, ha='right' if len(genotypes) > 3 else 'center')
    ax.set_xlim(-.5, len(genotypes) - .5); ax.set_ylabel('Dark − light (°C)', color=INK)
    handles = [Line2D([0], [0], color=group_colors[g], lw=2, label=f'{g} (n={len(ids_by_group[g])})')
               for g in genotypes]
    fig.legend(handles=handles, loc='upper center', bbox_to_anchor=(.5, .97), ncol=min(4, len(genotypes)),
               frameon=False, labelcolor='linecolor', fontsize=9)
    for suffix in ('.png', '.pdf', '.svg'):
        fig.savefig(out.with_suffix(suffix), dpi=300)
    plt.close(fig)
    return paired


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_files', nargs='+', type=Path,
                        help='One native Seek thermal CSV (full camera frames) per mouse.')
    parser.add_argument('--groups', nargs='+', required=True,
                        help='Group/genotype label for each CSV, in the same order as the file paths.')
    parser.add_argument('--mouse-ids', nargs='+',
                        help='Optional display ID for each CSV, in the same order; defaults to parent folder name.')
    parser.add_argument('--output', type=Path, default=Path('temperature_summary_24h.png'),
                        help='Output PNG path; PDF and SVG are saved beside it.')
    parser.add_argument('--timezone', default='Asia/Jerusalem', help='IANA timezone (default: Asia/Jerusalem).')
    parser.add_argument('--lights-on', default='10:30', help='Local lights-on time (default: 10:30).')
    parser.add_argument('--lights-off', default='22:30', help='Local lights-off time (default: 22:30).')
    parser.add_argument('--window-start', help='Optional local start YYYY-MM-DDTHH:MM[:SS]; otherwise first CSV frame.')
    args = parser.parse_args(argv)
    if len(args.groups) != len(args.csv_files):
        parser.error('provide exactly one --groups label for each input CSV, in the same order.')
    if args.mouse_ids is not None and len(args.mouse_ids) != len(args.csv_files):
        parser.error('provide exactly one --mouse-ids value for each input CSV.')
    items = []
    for index, raw_path in enumerate(args.csv_files):
        path = raw_path.resolve()
        mouse_id = args.mouse_ids[index].strip() if args.mouse_ids else path.parent.name or path.stem
        group = args.groups[index].strip()
        if not mouse_id or not group:
            parser.error('mouse IDs and group labels must be non-empty.')
        items.append({'path': path, 'mouse_id': mouse_id, 'genotype': group})
    identities = [item['mouse_id'].casefold() for item in items]
    if len(identities) != len(set(identities)):
        raise ValueError('Each input CSV must describe a different mouse_id.')
    tz = ZoneInfo(args.timezone)
    parse_clock(args.lights_on); parse_clock(args.lights_off)
    start = parse_start(args.window_start, args.timezone)
    if start is None:
        start = datetime.fromtimestamp(first_timestamp(items[0]['path']) / NS, timezone.utc)
    start_ns = int(start.timestamp() * NS)
    output = args.output.resolve()
    per_mouse = {}
    for item in items:
        print(f'Reading {item["mouse_id"]} ({item["genotype"]}) and calculating 7x7 local means...', file=sys.stderr)
        samples = iter_one_hz_roi_means(item, start_ns)
        per_mouse[item['mouse_id']] = reduce_profile(samples, item)
    output.parent.mkdir(parents=True, exist_ok=True)
    paired = draw_figure(per_mouse, start, tz.key, args.lights_on, args.lights_off, output)
    csv_path = output.with_name(output.stem + '_profiles.csv')
    with csv_path.open('w', encoding='utf-8', newline='') as handle:
        handle.write(f'# Window start: {start.astimezone(tz).isoformat()} ({tz.key})\n')
        writer = csv.writer(handle)
        writer.writerow(['mouse_id', 'genotype', 'half_hour', 'absolute_temperature_c',
                         'temperature_change_c', 'baseline_c', 'valid_bins'])
        for mouse_id, row in per_mouse.items():
            for index, (absolute, change) in enumerate(zip(row['absolute'], row['change'])):
                writer.writerow([mouse_id, row['genotype'], index, absolute, change, row['baseline'], row['valid_bins']])
    print(f'Figure saved to {output}, plus PDF/SVG and {csv_path}')
    print('Dark minus light (°C): ' + ', '.join(f'{group}={float(values.mean()):.3f}'
          for group, values in paired.items()))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(2)
