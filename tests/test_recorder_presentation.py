"""Recorder workflows survive layout changes; status explains backend gates."""
from concurrent.futures import Future
from types import SimpleNamespace
import threading

import pytest

from recorder_ui import readiness
from test_laptop_layout import window, pump


def state(**changes):
    values = dict(lock=threading.RLock(), state='idle', _writer_unhealthy=False,
                  _recovery_busy=lambda: False, prepare_done=threading.Event(),
                  options={'recording_mode': 'seek_only', 'microphone': False},
                  seek_ready=True, flir_cams=[], _audio_ready=lambda: True,
                  shutter_settings={'automatic': True}, ready=lambda: True)
    values.update(changes)
    result = SimpleNamespace(**values)
    result.prepare_done.set()
    return result


@pytest.mark.parametrize('changes,sample,expected,target', [
    ({}, 'custom', True, ''),
    ({'seek_ready': False}, 'custom', False, 'devices'),
    ({'options': {'recording_mode': 'flir_mp4', 'microphone': False}}, 'custom', False, 'devices'),
    ({'options': {'recording_mode': 'microphone_mp3', 'microphone': True}, '_audio_ready': lambda: False}, 'custom', False, 'microphone'),
    ({}, 'after_shutter', False, 'seek'),
    ({'shutter_settings': {'automatic': False}}, 'after_shutter', True, ''),
    ({'state': 'finalizing'}, 'custom', False, ''),
    ({'state': 'starting'}, 'custom', False, ''),
    ({'_writer_unhealthy': True}, 'custom', False, 'log'),
    ({'_recovery_busy': lambda: True}, 'custom', False, 'log'),
])
def test_readiness_explains_selected_mode(changes, sample, expected, target):
    controller = state(**changes)
    before = controller.options.copy()
    ready, message, action = readiness(controller, sample)
    assert ready is expected and action == target and message
    assert controller.options == before


def test_advanced_controls_and_mode_values_are_preserved(window):
    app, win = window
    win.record_mode.setCurrentIndex(4)
    win.seek_section.toggle.setChecked(True)
    win.sample_hz.setValue(2.5)
    win.calibration.setCurrentIndex(1)
    calibration = win.calibration.currentData()
    assert win.sample_mode.isVisible() and win.shutter_mode.isVisible()
    assert all(widget.isVisible() for widget in win.preview_actions)
    win.record_mode.setCurrentIndex(3)
    assert not win.seek_section.isVisible()
    assert win.mic_controls.isVisible()
    assert 'microphone' in win.readiness_label.text().lower()
    win.record_mode.setCurrentIndex(4)
    assert win.sample_hz.value() == 2.5
    assert win.calibration.currentData() == calibration
    win.sample_mode.setCurrentIndex(2)
    assert not win.record.isEnabled()
    assert 'Custom shutter' in win.readiness_label.text()
    win._toggle_settings()
    win.readiness_action.click()
    assert win.settings_scroll.isVisible() and win.shutter_mode.isVisible()
    from PySide6 import QtCore
    pump(app, lambda: win.settings_scroll.viewport().rect().contains(
        win.shutter_mode.mapTo(win.settings_scroll.viewport(), win.shutter_mode.rect().center())))
    win.shutter_mode.setCurrentIndex(1)
    win._apply_shutter()
    pump(app, lambda: win.record.isEnabled())
    win._reset_layout()
    assert win.sample_hz.value() == 2.5 and win.calibration.currentData() == calibration
    assert not win.seek_section.toggle.isChecked()


def test_live_audio_reuses_widget_during_recording_and_reports_saved(window):
    app, win = window
    win.record_mode.setCurrentIndex(1)
    win.mode.setCurrentIndex(1)
    win.auto_mp3.setChecked(False)
    win.toggle_connection()
    pump(app, lambda: win.engine is not None and win.record.isEnabled())
    live, engine, source = win.live, win.engine, win.source
    win.command('START')
    pump(app, lambda: win.controller.state == 'recording')
    win.audio_toggle.setChecked(True)
    assert win.audio_dock.widget() is live.plot
    win.tabs.setCurrentIndex(1)
    assert live.plot.parentWidget() is live
    win.tabs.setCurrentIndex(0)
    assert win.audio_dock.widget() is live.plot
    win._toggle_preview_mode()
    assert not win.audio_dock.isVisible() and win.stop.isVisible()
    win._escape_view()
    assert win.audio_dock.isVisible()
    assert (win.live, win.engine, win.source) == (live, engine, source)
    assert win.controller.is_recording
    pump(app, lambda: all(win.controller.take['frame_counts'].get(name, 0) >= 2
                         for name in ('seek', 'flir_cam0', 'flir_cam1'))
         and win.engine.snapshot()['recorded_samples'] > 40000)
    win.command('STOP')
    pump(app, lambda: win.controller.finished.is_set() and 'Saved' in win.result_label.text())
    assert win.controller.take['status'] == 'complete'
    assert win.open_recording.isEnabled()
    assert 'fixed for this run' in win.lock_note.text()


def test_warning_and_export_failure_remain_visible_in_preview_mode(window):
    app, win = window
    win.core.ui_events.put(('writer_warning', 'Synthetic low disk space warning'))
    pump(app, lambda: 'low disk' in win.readiness_label.text())
    win._toggle_preview_mode()
    assert win.readiness_label.isVisible() and win.dismiss_notice.isVisible()
    win.dismiss_notice.click()
    assert 'low disk' not in win.readiness_label.text()
    future = Future()
    future.set_exception(OSError('Synthetic disk failure'))
    win.exports['synthetic'] = future
    win.tick()
    assert 'MP3 export failed' in win.readiness_label.text()
    win.readiness_action.click()
    assert win.log_box.isVisible()
    assert 'WAV retained' in win.result_label.text()
