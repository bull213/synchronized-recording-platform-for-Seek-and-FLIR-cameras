"""Build, verify, and ZIP the combined recorder for another Windows x64 PC."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from ultramic_studio.recorder.cameras.runtime import find_seek_dll
from scripts.package_source import source_files

PACKAGES = ('numpy', 'opencv-python', 'PySide6', 'shiboken6', 'pyqtgraph',
            'sounddevice', 'pyserial', 'lameenc', 'spinnaker-python', 'pyinstaller')
VC_RUNTIMES = {'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
               'vcruntime140.dll', 'vcruntime140_1.dll', 'vcomp140.dll'}


def copy_editable_source(destination, source=SOURCE):
    """Copy only files in the reviewed source inventory."""
    destination = Path(destination)
    destination.mkdir()
    for name, path in source_files(source):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def isolated_environment():
    env = dict(os.environ)
    for key in ('PYTHONHOME', 'PYTHONPATH', 'SYNC_SEEK_DLL', 'GENICAM_GENTL64_PATH',
                'GENICAM_GENTL32_PATH', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
        env.pop(key, None)
    system_root = env.get('SystemRoot') or env.get('SYSTEMROOT') or os.environ.get('SystemRoot', r'C:\Windows')
    env['PATH'] = os.pathsep.join([str(Path(system_root) / 'System32'), system_root])
    return env


def audit_native_files(folder):
    """Require x64 native files and all non-Windows imports inside the bundle."""
    import pefile
    native = [p for p in (folder / '_internal').rglob('*')
              if p.suffix.lower() in ('.dll', '.pyd', '.cti')]
    for path in tuple(native):
        if '_sounddevice_data' in path.parts and path.name.lower().startswith(('libportaudio32bit', 'libportaudioarm64')):
            # A standard PyInstaller hook can add these after collect_all.
            # Delete only these known unused files in this generated bundle.
            path.unlink()
            native.remove(path)
    names = {p.name.lower() for p in native}
    system = Path(os.environ['SystemRoot']) / 'System32'
    missing = set()
    for path in native:
        pe = pefile.PE(str(path), fast_load=True)
        try:
            if pe.FILE_HEADER.Machine != 0x8664:
                raise RuntimeError(f'Non-x64 native file: {path.name}')
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
            for item in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', []):
                name = item.dll.decode('ascii').lower()
                if name in names or name.startswith(('api-ms-', 'ext-ms-')):
                    continue
                if name not in VC_RUNTIMES and (system / name).is_file():
                    continue
                missing.add(f'{path.name} -> {name}')
        finally:
            pe.close()
    if missing:
        raise RuntimeError('Missing portable native dependencies: ' + '; '.join(sorted(missing)))
    return len(native)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, default=SOURCE / 'exports')
    parser.add_argument('--seek-driver', type=Path,
                        help='Folder containing seekusb.inf and seekusb.cat')
    parser.add_argument('--flir-driver', type=Path,
                        default=Path(r'C:\Program Files\Teledyne\Spinnaker\driver64\PGRUsb3'),
                        help='Folder containing the installed vendor FLIR USB3 driver')
    args = parser.parse_args()
    approved = source_files(SOURCE)
    if platform.system() != 'Windows' or struct.calcsize('P') != 8:
        raise RuntimeError('Build on Windows x64, using the configured camera Python environment.')
    import PySpin  # Verify that the matching vendor package is installed.
    seek = find_seek_dll()
    seek_driver = args.seek_driver
    if seek_driver is None:
        seek_driver = next((p / 'driver' for p in seek.parents if (p / 'driver' / 'seekusb.inf').is_file()), None)
    if seek_driver is None or not (seek_driver / 'seekusb.cat').is_file():
        raise RuntimeError('Seek driver package not found. Pass --seek-driver with the licensed SDK driver folder.')
    required_flir = ('PGRUSBCam3.inf', 'pgrusbcam3.cat', 'PGRUSBCam3.sys', 'WdfCoInstaller01009.dll')
    if any(not (args.flir_driver / name).is_file() for name in required_flir):
        raise RuntimeError('FLIR USB3 driver package not found. Pass --flir-driver with its installed SDK folder.')
    versions = {name: importlib.metadata.version(name) for name in PACKAGES}
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    build = SOURCE / 'build' / ('SyncRecorder_' + stamp)
    output = args.output_root.resolve() / ('SyncRecorder_' + stamp)
    build.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    log = build / 'build.log'
    spec = build / 'SyncRecorder.spec'
    # Explicit VC runtime binaries keep a fresh PC independent of the build PC's
    # installed Visual Studio redistributables. No debug C/C++ runtime is needed.
    vc_files = []
    roots = [Path(sys.base_prefix), Path(os.environ['SystemRoot']) / 'System32', Path(PySpin.__file__).parent]
    for name in sorted(VC_RUNTIMES):
        path = next((root / name for root in roots if (root / name).is_file()), None)
        if path:
            vc_files.append((str(path), '.'))
    spec.write_text(f'''from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata
datas = [({str(SOURCE / 'ultramic_studio' / 'assets' / 'check.svg')!r}, 'ultramic_studio/assets')]
binaries = [({str(seek)!r}, 'camera_sdk/seek')] + {vc_files!r}
hiddenimports = ['ultramic_studio.winmm']
for package in ('PySpin', 'sounddevice', 'lameenc'):
    data, native, hidden = collect_all(package)
    # sounddevice ships both architectures; this app targets Windows x64 only.
    data = [item for item in data if not any(name in item[0].lower() for name in ('libportaudio32bit', 'libportaudioarm64'))]
    native = [item for item in native if not any(name in item[0].lower() for name in ('libportaudio32bit', 'libportaudioarm64'))]
    datas += data
    binaries += native
    hiddenimports += hidden
datas += collect_data_files('pyqtgraph')
for package in {list(PACKAGES[:-1])!r}:
    datas += copy_metadata(package)
a = Analysis([{str(SOURCE / 'launch_sync.py')!r}], pathex=[{str(SOURCE)!r}],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             runtime_hooks=[{str(SOURCE / 'scripts' / 'sync_runtime_hook.py')!r}],
             excludes=['pytest', 'tkinter'], noarchive=False)
a.binaries = [item for item in a.binaries if not any(name in item[0].lower() for name in ('libportaudio32bit', 'libportaudioarm64'))]
a.datas = [item for item in a.datas if not any(name in item[0].lower() for name in ('libportaudio32bit', 'libportaudioarm64'))]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SyncRecorder',
          console=True, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='SyncRecorder', strip=False, upx=False)
''', encoding='utf-8')
    print(f'Building the app. Build log: {log}', flush=True)
    with log.open('w', encoding='utf-8') as handle:
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(output),
                        '--workpath', str(build / 'work'), str(spec)], cwd=SOURCE,
                       stdout=handle, stderr=subprocess.STDOUT, check=True)
    folder = output / 'SyncRecorder'
    for name in ('Start_Sync_Recorder.cmd', 'Setup_New_PC.cmd', 'Install_Camera_Drivers.cmd',
                 'Demo_Sync_Recorder.cmd', 'START_SYNC_HERE.html', 'README.md',
                 'Start_Sync_Recorder.ps1', 'Create_Desktop_Shortcut.ps1', 'Create_Desktop_Shortcut.cmd'):
        shutil.copy2(SOURCE / name, folder / name)
    for name, path in approved:
        if name.startswith('docs/') or name == 'LICENSE':
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    shutil.copytree(seek_driver, folder / 'drivers' / 'Seek')
    shutil.copytree(args.flir_driver, folder / 'drivers' / 'FLIR_USB3')
    copy_editable_source(folder / 'source')
    licenses = folder / 'licenses'
    licenses.mkdir()
    shutil.copy2(Path(sys.base_prefix) / 'LICENSE.txt', licenses / 'Python_LICENSE.txt')
    shutil.copy2(Path(PySpin.__file__), licenses / 'PySpin_NOTICE.py')
    seek_license = next((candidate for parent in seek.parents
                         for candidate in (parent / 'LICENSE.txt', parent / 'licenses' / 'Seek_SDK_LICENSE.txt')
                         if candidate.is_file()), None)
    if seek_license:
        shutil.copy2(seek_license, licenses / 'Seek_SDK_LICENSE.txt')
    shutil.copytree(SOURCE / 'docs' / 'licenses', licenses / 'additional')
    # Do not expose the build PC's local wheel/source paths in pip metadata.
    for path in (folder / '_internal').glob('*.dist-info/direct_url.json'):
        path.unlink()
    print('Checking native dependencies and the exported EXE...', flush=True)
    count = audit_native_files(folder)
    exe = folder / 'SyncRecorder.exe'
    for option, filename in (('--check-setup', 'build_setup_report.txt'), ('--self-test', 'build_self_test_report.txt')):
        subprocess.run([str(exe), option, '--report', filename], cwd=folder,
                       env=isolated_environment(), timeout=90, check=True)
    manifest = dict(application='Seek / FLIR / Ultramic Recorder', target='Windows 10/11 x64',
                    built_utc=datetime.now(timezone.utc).isoformat(), python=sys.version.split()[0],
                    packages=versions, seek_dll_sha256=digest(seek), native_files_checked=count,
                    drivers=['Seek WinUSB', 'FLIR USB3'],
                    software_checks='passed with Python/SDK search paths removed; hardware acquisition unverified')
    (folder / 'BUILD_INFO.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    archive = output / ('SyncRecorder_Windows_x64_' + stamp + '.zip')
    print('Creating the transfer ZIP...', flush=True)
    with ZipFile(archive, 'x', ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                zipped.write(path, Path('SyncRecorder') / path.relative_to(folder))
    checksum = digest(archive)
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    (args.output_root / 'LATEST_EXPORT.txt').write_text(str(archive) + '\n', encoding='utf-8')
    print(f'EXPORT READY: {archive}\nSize: {archive.stat().st_size / 1_000_000:.1f} MB\nSHA256: {checksum}', flush=True)


if __name__ == '__main__':
    main()
