# Third-party components

The original application source is MIT-licensed; see [LICENSE](../LICENSE).
This source distribution contains no Python runtime, installed wheels, camera SDKs,
drivers, pretrained models, or research datasets. Dependency licenses continue to
apply when users install or distribute those components.

| Component | Upstream |
| --- | --- |
| CPython | https://www.python.org/ |
| NumPy | https://github.com/numpy/numpy |
| PySide6 / Shiboken6 | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Qt | https://code.qt.io/cgit/qt/ |
| pyqtgraph | https://github.com/pyqtgraph/pyqtgraph |
| sounddevice | https://github.com/spatialaudio/python-sounddevice |
| PortAudio | https://github.com/PortAudio/portaudio |
| pyserial | https://github.com/pyserial/pyserial |
| lameenc / LAME | https://github.com/chrisstaite/lameenc |
| OpenCV | https://github.com/opencv/opencv |
| pytest | https://github.com/pytest-dev/pytest |
| PyInstaller | https://github.com/pyinstaller/pyinstaller |

Seek SDK and Teledyne Spinnaker/PySpin are installed separately under their vendor
terms. The unrelated PyPI package named pyspin is not the camera SDK.

The retained LGPL/GPL texts in [licenses](licenses/) are dependency reference
materials, not a replacement for each component's supplied notices and exact license.
Installed wheel metadata contains the applicable component notices. Generated
portable builds must retain those notices and any required vendor licenses.

The optional combined transfer builder copies locally installed vendor libraries
and drivers for use on another appropriately licensed recording PC. Its existence
does not grant redistribution rights for those files. Public binary releases require
a separate review; this repository publishes source only.
