"""Launch the combined recorder; --demo-cameras never opens camera hardware."""
from pathlib import Path
import runpy
import sys


def main():
    if '--check-setup' in sys.argv or '--self-test' in sys.argv:
        from recorder_setup import check_setup, self_test
        report = None
        if '--report' in sys.argv:
            report = Path(sys.argv[sys.argv.index('--report') + 1])
        return self_test(report) if '--self-test' in sys.argv else check_setup(report)
    if '--demo-cameras' in sys.argv:
        from camera_demo import DemoCameraCore
        from camera_recorder_gui import RecorderApp
        return RecorderApp(DemoCameraCore()).run()
    runpy.run_path(str(Path(__file__).with_name('sync-rec.py')), run_name='__main__')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'Startup failed: {exc}', file=sys.stderr)
        print('See docs/SETUP.md for source setup; a bundled EXE also includes Setup_New_PC.cmd.', file=sys.stderr)
        raise SystemExit(1)
