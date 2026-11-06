"""Launch and export regressions: exercise the app outside the source folder."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ultramic_studio.recorder import cli, diagnostics, paths

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('option,action', [('--self-test', 'self_test'), ('--check-setup', 'check_setup')])
def test_diagnostic_dispatch_preserves_report_path_and_exit_code(tmp_path, monkeypatch, option, action):
    received = []
    report = tmp_path / 'report with spaces.txt'
    monkeypatch.setattr(diagnostics, action, lambda path: received.append(path) or 7)
    assert cli.main([option, '--report', str(report)]) == 7
    assert received == [report]


@pytest.mark.parametrize('args', [
    ['--report'], ['--report', 'file.txt'], ['--self-test', '--check-setup'], ['--unknown'],
])
def test_invalid_arguments_fail_before_starting_the_app(args, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    assert 'error:' in capsys.readouterr().err


@pytest.mark.parametrize('demo', [False, True])
def test_gui_dispatch_passes_explicit_options_and_returns_event_loop_code(monkeypatch, demo):
    from ultramic_studio.recorder import reliability
    from ultramic_studio.recorder.cameras import capture, demo as demo_module
    from ultramic_studio.recorder.ui import window

    received = []
    demo_core = object()
    monkeypatch.setattr(reliability, 'configure_recording_logging', lambda: None)
    monkeypatch.setattr(demo_module, 'DemoCameraCore', lambda: demo_core)

    class App:
        def __init__(self, core, *, demo_audio):
            received.append((core, demo_audio))

        def run(self):
            return 9

    monkeypatch.setattr(window, 'RecorderApp', App)
    args = ['--demo-audio'] + (['--demo-cameras'] if demo else [])
    assert cli.main(args) == 9
    assert received == [(demo_core if demo else capture, True)]


def test_startup_exception_is_reported(monkeypatch, capsys):
    def fail(report):
        raise RuntimeError('diagnostic failure')

    monkeypatch.setattr(diagnostics, 'self_test', fail)
    assert cli.main(['--self-test']) == 1
    assert 'Startup failed: diagnostic failure' in capsys.readouterr().err


def test_source_paths_are_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(vars(sys), 'frozen', raising=False)
    monkeypatch.delitem(vars(sys), '_MEIPASS', raising=False)
    assert paths.application_folder() == ROOT
    assert paths.resource_folder() == ROOT


def test_cli_import_is_lightweight():
    result = subprocess.run([sys.executable, '-c',
        'import sys; from ultramic_studio.recorder import cli; '
        'assert not any(name.startswith(("PySide6", "PySpin", "cv2", '
        '"ultramic_studio.recorder.cameras")) for name in sys.modules)'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_exported_source_imports_and_launches_without_root_modules(tmp_path):
    from scripts.build_sync_portable import copy_editable_source

    destination = tmp_path / 'Moved recorder source'
    copy_editable_source(destination)
    assert not list(destination.rglob('__pycache__'))
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-c',
        'from pathlib import Path; '
        'from ultramic_studio.recorder.cameras import capture; '
        'from ultramic_studio.recorder.ui.window import RecorderApp; '
        'from ultramic_studio.recorder import coordinator, diagnostics, health, storage; '
        'from thermal_roi import export_rois; from thermal_roi_gui import RoiWindow; '
        'from thermal_roi_video import export_video_parts; '
        'from ultramic_studio.recorder.paths import application_folder; '
        'assert application_folder() == Path.cwd(); '
        'assert capture.seek is None and capture.PySpin is None; '
        'assert Path(capture.__file__).is_relative_to(Path.cwd())'],
        cwd=destination, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    for entry in (['-m', 'ultramic_studio.recorder'], ['launch_sync.py'], ['sync-rec.py']):
        result = subprocess.run([sys.executable, *entry, '--help'], cwd=destination,
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert '--demo-cameras' in result.stdout


@pytest.mark.skipif(os.name != 'nt' or not (ROOT / '.venv/Scripts/python.exe').exists(),
                    reason='Requires the Windows source launcher environment')
@pytest.mark.parametrize('args,expected', [(['--help'], 0), (['--report'], 2)])
def test_cmd_launcher_from_another_folder_preserves_exit_code(tmp_path, args, expected):
    result = subprocess.run(['cmd.exe', '/d', '/c', str(ROOT / 'Start_Sync_Recorder.cmd'), *args],
                            cwd=tmp_path, input='\n', capture_output=True, text=True, timeout=30)
    assert result.returncode == expected, result.stdout + result.stderr
