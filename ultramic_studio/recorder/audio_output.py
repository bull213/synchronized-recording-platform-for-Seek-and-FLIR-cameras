"""Finalize microphone-only output without owning recording state or threads."""
from __future__ import annotations

import json
import shutil


def finish_mp3(take, run_dir, take_number, audio_staging, *, encode):
    """Export the take and remove its private staging directory on success.

    Return whether a session was finalized. The supplied take remains the
    coordinator's live report, including the path recorded before cleanup.
    """
    if 'audio_path' not in take:
        return False
    session = run_dir / take['audio_path']
    meta = json.loads((session / 'session.json').read_text(encoding='utf-8'))
    wavs = [session / item['file'] for item in meta['segments']]
    if any(path.resolve().parent != session.resolve() for path in wavs):
        raise ValueError('Invalid audio segment path.')
    target = run_dir / f'microphone_take{take_number:02d}.mp3'
    try:
        encode(wavs, target, time_expansion=1, write_metadata=False)
    except Exception as exc:
        raise RuntimeError(f'MP3 export failed; recoverable WAV retained at {session}: {exc}') from exc
    take['mp3_path'] = target.name
    staging = audio_staging.resolve()
    if staging.parent != run_dir.resolve() or not staging.name.startswith('.audio_'):
        raise RuntimeError('Unexpected temporary audio directory; retained for recovery.')
    shutil.rmtree(staging)
    return True
