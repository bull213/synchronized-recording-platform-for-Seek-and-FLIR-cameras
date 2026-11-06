"""Command-line dispatch with lazy GUI and camera imports."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def _ensure_console_streams():
    """Keep diagnostics available when started with pythonw.exe."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    root = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'SeekCameraRecorder' / 'logs'
    root.mkdir(parents=True, exist_ok=True)
    stream = (root / 'recorder.log').open('a', encoding='utf-8', buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _parse_args(argv):
    parser = argparse.ArgumentParser(description='Seek / FLIR / Ultramic recorder')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check-setup', action='store_true', help='Check installed libraries and devices')
    action.add_argument('--self-test', action='store_true', help='Test recording with synthetic data')
    action.add_argument('--demo-cameras', action='store_true', help='Use synthetic cameras without camera hardware')
    parser.add_argument('--demo-audio', action='store_true', help='Start synthetic microphone monitoring')
    parser.add_argument('--report', type=Path, help='Write setup or self-test results to this file')
    args = parser.parse_args(argv)
    if args.report is not None and not (args.check_setup or args.self_test):
        parser.error('--report requires --check-setup or --self-test')
    return args


def main(argv=None):
    """Run the selected action and return its exit code."""
    _ensure_console_streams()
    args = _parse_args(argv)
    try:
        if args.check_setup or args.self_test:
            from .diagnostics import check_setup, self_test
            return self_test(args.report) if args.self_test else check_setup(args.report)

        from .reliability import configure_recording_logging
        configure_recording_logging()
        if args.demo_cameras:
            from .cameras.demo import DemoCameraCore
            core = DemoCameraCore()
        else:
            from .cameras import capture
            core = capture
        from .ui.window import RecorderApp
        return RecorderApp(core, demo_audio=args.demo_audio).run()
    except Exception as exc:
        print(f'Startup failed: {exc}', file=sys.stderr)
        print('Run Setup_New_PC.cmd in the extracted app folder for diagnostics.', file=sys.stderr)
        return 1
