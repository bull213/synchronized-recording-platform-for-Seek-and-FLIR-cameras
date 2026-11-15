# Bundled components

The portable distribution includes unmodified official Windows Python binaries
and PyPI wheels. Their versions and archive SHA-256 hashes are recorded in
`PORTABLE_BUILD.json`. Python's license is in `runtime/LICENSE.txt`; wheel
metadata and their supplied notices remain under `runtime/Lib/site-packages`
in each distribution's `.dist-info` directory. Additional LGPL/GPL license texts
are in `docs/licenses`.

| Component | Upstream project / source |
| --- | --- |
| CPython | https://www.python.org/downloads/release/python-31316/ |
| PySide6 / Shiboken6 | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Qt 6 | https://code.qt.io/cgit/qt/ |
| NumPy | https://github.com/numpy/numpy |
| pyqtgraph | https://github.com/pyqtgraph/pyqtgraph |
| sounddevice | https://github.com/spatialaudio/python-sounddevice |
| PortAudio | https://github.com/PortAudio/portaudio |
| pyserial | https://github.com/pyserial/pyserial |
| lameenc | https://github.com/chrisstaite/lameenc |
| LAME | https://lame.sourceforge.io/ |
| CFFI | https://foss.heptapod.net/pypy/cffi |
| pycparser | https://github.com/eliben/pycparser |
| colorama | https://github.com/tartley/colorama |

The application's editable Python source is in `ultramic_studio`. It loads
separate shared libraries; the bundled dependency directories can be replaced
with compatible builds. No dependency libraries have been statically linked
into a new proprietary application executable. Component licenses and notices
continue to apply independently to their respective files.

The combined camera recorder's development environment uses the separate `.venv`,
including OpenCV from `requirements-camera.txt` and the user's Seek and
Spinnaker/PySpin SDK installations. These are separate from the original standalone
portable-runtime build manifest.

`Export_Sync_Recorder.cmd` creates a personal transfer bundle with the installed
camera runtime libraries and Seek/FLIR USB3 driver packages for another licensed
recording PC. Installed Python/package metadata and license notices are preserved;
the Python license, Seek SDK license, PySpin notice, and additional library notices
are included under `licenses` or `_internal/*.dist-info`. SDK/driver terms continue
to apply. `BUILD_INFO.json` records versions and the Seek DLL hash. This export
does not change the original SDK/driver packages or grant redistribution rights.
