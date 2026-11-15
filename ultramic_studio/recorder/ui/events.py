"""Camera, recorder, and background event handling for the UnifiedWindow."""
from __future__ import annotations

import queue
from pathlib import Path


def handle_camera_events(win) -> None:
    """Drain and dispatch all pending core UI events for the recorder window."""
    while True:
        try:
            event = win.core.ui_events.get_nowait()
        except queue.Empty:
            break
        dispatch_camera_event(win, event)


def dispatch_camera_event(win, event: tuple) -> None:
    """Dispatch a single recorder UI event to its appropriate handler."""
    kind = event[0]
    if kind == 'connected':
        _on_cameras_connected(win, event[1])
    elif kind == 'connect_error':
        _on_connect_error(win, event[1])
    elif kind == 'recording_pending':
        _on_recording_pending(win)
    elif kind == 'recording_started':
        win.log('Recording selected streams.')
    elif kind == 'recording_finalizing':
        win.log('Stopping at the shared boundary and finishing files…')
    elif kind == 'recording_saved':
        _on_recording_saved(win, event)
    elif kind in ('camera_error', 'writer_error', 'seek_disconnected'):
        _on_device_error(win, kind, event)
    elif kind in ('message', 'writer_warning'):
        win.log(event[1])
        if kind == 'writer_warning':
            win._show_notice(event[1])
    elif kind == 'seek_event':
        win.log(f'Seek {event[2]}: {event[3]}')
    elif kind == 'shutdown_complete':
        win._cameras_closed = True


def _on_cameras_connected(win, camera_list: list[dict]) -> None:
    prefix = 'DEMO cameras · ' if getattr(win.core, 'demo', False) else ''
    seek = 'Seek + ' if win.controller.seek_ready else ''
    win.camera_status.setText(prefix + f'{seek}{len(camera_list)} FLIR camera(s) connected')
    for index, camera in enumerate(camera_list, 1):
        key = f"flir_{camera['index']}"
        if key not in win._preview_labels:
            win._add_camera_panel(key, f"FLIR {camera['index']} · {camera['serial']}", index)
    win.log(win.camera_status.text())


def _on_connect_error(win, error_msg: str) -> None:
    win.camera_status.setText('Camera connection failed')
    win._show_notice(error_msg, 'error')
    win.log(error_msg)


def _on_recording_pending(win) -> None:
    win._audio_saved = None
    win.log('Record requested: ' + win.record_mode.currentText())
    win.settings.setValue('output', win.output.text())
    win.settings.setValue('label', win.session_label.text())


def _on_recording_saved(win, event: tuple) -> None:
    win.drain_notices()
    status, run_dir, take = event[2], event[1], event[3]
    win._last_result_take = take['take_id']
    result_text = {'complete': 'Saved', 'complete_with_gaps': 'Saved with gaps', 'error': 'Failed'}.get(status, status)
    win._last_result = f"Take {take['take_id']} · {result_text}"
    win._export_status = ''
    if status != 'complete':
        win._show_notice(win._last_result + '. Check diagnostics and the take report.',
                         'error' if status == 'error' else 'warning')
    win.log(f"Take {take['take_id']}: {status}. Folder: {run_dir}")
    if take.get('recording_mode', 'combined') != 'combined':
        win.sync_summary.setText(f"Take {take['take_id']}: {status}. Folder: {run_dir}")
        win.sync_summary.setToolTip('')
        if take.get('mp3_path'):
            win._last_mp3 = Path(run_dir) / take['mp3_path']
            win.last_session = None
            win.log(f'Normal-speed MP3 ready: {win._last_mp3}')
        for error in take.get('errors', []):
            win.log(error)
        return

    parts = [f"{name}: {item['offset_from_seek_ns'] / 1e6:+.3f} ms" +
             (' (estimate)' if name == 'microphone' else '')
             for name, item in take.get('first_streams', {}).items()]
    win.sync_summary.setText('First saved offsets from Seek — ' + ' | '.join(parts) +
                             f". Report: take{take['take_id']:02d}_sync.json")
    win.sync_summary.setToolTip(take.get('timing_note', ''))
    if status == 'complete' and take.get('audio_path'):
        win.last_session = Path(run_dir) / take['audio_path']
        win._last_mp3 = None
        if len(event) > 4 and event[4]:
            win.queue_export(win.last_session)


def _on_device_error(win, kind: str, event: tuple) -> None:
    message = event[1] if len(event) > 1 else 'Seek disconnected.'
    win._show_notice(message, 'error')
    mode = win.controller.options['recording_mode']
    seek_error = kind == 'seek_disconnected' or str(message).startswith('Seek')
    if kind == 'writer_error' or mode == 'combined' or (mode == 'flir_mp4' and not seek_error) or (mode == 'seek_only' and seek_error):
        win.controller.fault(str(message))
    else:
        win.log(str(message))
    if kind == 'seek_disconnected':
        win.controller.cameras_ready = False
        win.controller.seek_ready = False
        win.camera_status.setText('Seek disconnected · restart after reconnecting')
