# Development and local builds

## Recorder checks

Recorder acquisition, coordination, UI, and cage ROI tools live under
`ultramic_studio/recorder/`. Existing root-level module names are compatibility
aliases. The combined recorder starts with `python -m ultramic_studio.recorder`
or `Start_Sync_Recorder.cmd`; the standalone ROI launcher remains available.

Install Python 3.12 x64 and the complete development requirements:

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-dev.txt
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m pytest -q -rs -p no:cacheprovider
.venv\Scripts\python.exe launch_sync.py --self-test
```

Tests use temporary outputs, isolated Qt preferences, mocked SDK interfaces,
and synthetic media. Physical cameras, microphones, and vendor SDKs are not required.
The suite checks PCM preservation, trigger behavior, shared timing, camera resource
cleanup, segmented outputs, calibration with fictional values, recovery and gap
reports, and GUI workflows.

The POSIX serial integration test skips on Windows. Independent MP3 decoding
requires FFmpeg on PATH and skips when unavailable; the app uses lameenc for encoding.
GitHub Actions repeats the checks on Windows with Python 3.12.

## Source releases

`SOURCE_FILES.txt` lists the reviewed publication inventory. Add new public files
explicitly and review their contents; the inventory is not a credential detector.

```bat
.venv\Scripts\python.exe scripts/package_source.py --check
.venv\Scripts\python.exe scripts/package_source.py --output ..\UltramicStudio-source.zip
```

The packager preserves hidden Git files, excludes unlisted files, rejects unsafe
paths and linked files, refuses to overwrite exports, and emits a SHA-256 sidecar.
Write each ZIP outside the source directory. Review the final ZIP before publishing.
The same inventory supplies editable source in local portable builds.

Refresh screenshots using only synthetic inputs:

```bat
.venv\Scripts\python.exe scripts/capture_demo_screenshots.py
```

This renders the actual GUI using isolated preferences, temporary media, and generic
display paths. It creates the four documentation PNGs without opening hardware.

## Optional Windows builds

These recipes are for local builds. This source release contains no executables,
runtime libraries, SDK binaries, or vendor drivers.

For a standalone microphone executable, run `Build_Windows_App.cmd`.
Keep the entire resulting `dist/UltramicStudio` folder together. For a combined
recorder transfer bundle, first install your licensed camera SDKs, configure
`SYNC_SEEK_DLL`, then run:

```bat
Export_Sync_Recorder.cmd --seek-driver "PATH_TO_LICENSED_SEEK_DRIVER_FOLDER"
```

The combined builder needs the matching Spinnaker/PySpin installation and licensed
Seek/FLIR drivers. `--flir-driver` overrides the standard installed FLIR USB3 path.
It generates an EXE, checks native dependencies and synthetic recording, preserves
component notices, includes reviewed editable source, and writes a ZIP with checksums
under `exports/`. Hardware checks must be performed separately.

`scripts/build_portable.py` is the legacy standalone embedded-runtime recipe.
It requires the exact upstream runtime archive/hash declared in that script and
compatible downloaded wheels. Run it from a fresh source copy without `runtime/`;
its optional output ZIP must be outside the source folder.

Before distributing any binary build, separately review the redistribution terms
and required notices for every included dependency, SDK, and driver. The project's
MIT license does not license those components. Binary recipes were not exercised
as part of source-only release validation.
