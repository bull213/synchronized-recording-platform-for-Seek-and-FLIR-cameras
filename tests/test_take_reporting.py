"""Characterize the externally saved take and audio-output contracts."""
import json

import pytest

from ultramic_studio.recorder.audio_output import finish_mp3
from ultramic_studio.recorder.take_reporting import collect_audio_report, new_take_report


def make_report(microphone=True):
    return new_take_report(
        take_number=2,
        options={'label': 'session', 'recording_mode': 'combined', 'sample_mode': 'custom',
                 'microphone': microphone},
        info={'label': 'trial'}, request_ns=1000, seek_fps=2.5,
        calibration={'name': 'none', 'scale': 1.0, 'offset_c': 0.0,
                     'input_units': 'C', 'output_units': 'C'},
        shutter_settings={'automatic': True, 'hz': 1.0}, requested_utc='2026-01-01T00:00:00Z',
        audio_demo=False, camera_demo=True, flir_cams=[{'index': 0, 'serial': 'DEMO'}],
    )


def test_initial_report_preserves_public_fields_and_independent_collections():
    report = make_report()
    assert set(report) == {
        'schema_version', 'take_id', 'label', 'recording_mode', 'sample_mode', 'seek_video_fps',
        'seek_calibration', 'shutter_settings', 'status', 'requested_utc', 'request_monotonic_ns',
        'shared_start_monotonic_ns', 'shared_stop_monotonic_ns', 'microphone_enabled', 'audio_status',
        'audio_demo', 'camera_demo', 'first_streams', 'frame_counts', 'frame_id_gaps', 'events', 'errors',
        'event_count', 'events_truncated', 'event_log', 'received_frame_counts', 'written_frame_counts',
        'dropped_frame_counts', 'recovery_counts', 'has_gaps', 'gaps', 'gap_count', 'segments',
        'camera_serials', 'timing_note',
    }
    assert (report['schema_version'], report['take_id'], report['label'], report['status']) == (2, 2, 'trial', 'starting')
    assert report['request_monotonic_ns'] == 1000
    assert report['audio_status'] == 'pending' and report['camera_serials'] == {'0': 'DEMO'}
    assert report['event_log'] == 'take02_events.jsonl'
    report['errors'].append('one')
    assert make_report()['errors'] == []
    assert make_report(microphone=False)['audio_status'] == 'disabled'


def test_audio_report_preserves_sample_offset_and_missing_session_error(tmp_path):
    take = make_report()
    take.update(audio_path='microphone/take02', shared_start_monotonic_ns=1000)
    collect_audio_report(take, tmp_path)
    assert take['audio_status'] == 'error'
    assert take['errors'] == ['Audio session manifest is missing.']

    take['errors'].clear()
    session = tmp_path / take['audio_path']
    session.mkdir(parents=True)
    (session / 'session.json').write_text(json.dumps({
        'status': 'complete', 'recorded_samples': 500, 'separate_pretrigger_samples': 20,
        'first_saved_monotonic_ns': 1003, 'timestamp_source': 'synthetic',
    }), encoding='utf-8')
    collect_audio_report(take, tmp_path)
    assert (take['audio_status'], take['audio_samples'], take['separate_pretrigger_samples']) == ('complete', 500, 20)
    assert take['first_streams']['microphone'] == {
        'host_monotonic_ns': 1003, 'offset_from_seek_ns': 3,
        'timestamp_source': 'synthetic', 'file_sample': 0,
    }


def test_mp3_failure_retains_staging_and_success_removes_only_staging(tmp_path):
    staging = tmp_path / '.audio_fixture'
    session = staging / 'capture'
    session.mkdir(parents=True)
    (session / 'session.json').write_text(json.dumps({'segments': [{'file': 'audio.wav'}]}), encoding='utf-8')
    (session / 'audio.wav').write_bytes(b'synthetic fixture')
    take = {'audio_path': '.audio_fixture/capture'}

    def fail(*args, **kwargs):
        raise OSError('encoder unavailable')

    with pytest.raises(RuntimeError, match='recoverable WAV retained'):
        finish_mp3(take, tmp_path, 2, staging, encode=fail)
    assert (session / 'audio.wav').is_file()
    assert 'mp3_path' not in take

    def save(paths, destination, *, time_expansion, write_metadata):
        assert paths == [session / 'audio.wav']
        assert time_expansion == 1 and not write_metadata
        destination.write_bytes(b'synthetic mp3')

    assert finish_mp3(take, tmp_path, 2, staging, encode=save)
    assert (tmp_path / 'microphone_take02.mp3').is_file()
    assert take['mp3_path'] == 'microphone_take02.mp3'
    assert not staging.exists()
