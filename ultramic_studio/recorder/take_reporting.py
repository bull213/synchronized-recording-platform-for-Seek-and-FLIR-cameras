"""Create take reports and incorporate finalized microphone metadata.

The coordinator owns the take dictionary and calls these helpers from the same
threads and under the same locks as before. These functions do not start or stop
recording workers.
"""
from __future__ import annotations

import json


def new_take_report(*, take_number, options, info, request_ns, seek_fps,
                    calibration, shutter_settings, requested_utc,
                    audio_demo, camera_demo, flir_cams):
    """Create the initial externally visible synchronization report."""
    return dict(schema_version=2, take_id=take_number, label=info.get('label') or options['label'],
                recording_mode=options['recording_mode'], sample_mode=options['sample_mode'],
                seek_video_fps=seek_fps, seek_calibration=calibration,
                shutter_settings=dict(shutter_settings), status='starting', requested_utc=requested_utc,
                request_monotonic_ns=request_ns, shared_start_monotonic_ns=None,
                shared_stop_monotonic_ns=None, microphone_enabled=options['microphone'],
                audio_status='pending' if options['microphone'] else 'disabled',
                audio_demo=audio_demo, camera_demo=camera_demo,
                first_streams={}, frame_counts={}, frame_id_gaps={}, events=[], errors=[],
                event_count=0, events_truncated=False,
                event_log=f'take{take_number:02d}_events.jsonl',
                received_frame_counts={}, written_frame_counts={}, dropped_frame_counts={},
                recovery_counts={}, has_gaps=False, gaps=[], gap_count=0, segments={},
                camera_serials={str(cam['index']): cam['serial'] for cam in flir_cams},
                timing_note='Seek and FLIR times are host frame receipt times. Audio uses labelled '
                'capture-time estimates. Raw device clocks are independent; physical simultaneity is unverified.')


def collect_audio_report(take, run_dir):
    """Merge a finished audio session's status and timing into its take."""
    if not take['microphone_enabled'] or 'audio_path' not in take:
        return
    manifest = run_dir / take['audio_path'] / 'session.json'
    if not manifest.exists():
        take['audio_status'] = 'error'
        take['errors'].append('Audio session manifest is missing.')
        return
    meta = json.loads(manifest.read_text(encoding='utf-8'))
    take['audio_status'] = meta['status']
    take['audio_samples'] = meta['recorded_samples']
    take['separate_pretrigger_samples'] = meta.get('separate_pretrigger_samples', 0)
    first = meta.get('first_saved_monotonic_ns')
    if first is not None:
        take['first_streams']['microphone'] = dict(host_monotonic_ns=first,
            offset_from_seek_ns=first - take['shared_start_monotonic_ns'],
            timestamp_source=meta.get('timestamp_source'), file_sample=0)
    if meta['status'] != 'complete' or not meta['recorded_samples']:
        take['errors'].extend(meta.get('errors') or ['Audio contains no completed samples.'])
