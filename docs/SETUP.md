# Source setup

## Supported development setup

Use Windows 10/11 x64 and **Python 3.12 x64**. The source release is checked on
Python 3.12; matching vendor camera bindings may further constrain the interpreter.
The setup helper also accepts Python 3.11/3.13, but those versions are not covered
by this release's validation.

From Command Prompt in the source directory:

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
.venv\Scripts\python.exe -m ultramic_studio --demo
```

Alternatively, double-click `Setup_Windows.cmd`, followed by
`Start_Ultramic_Studio.cmd`. These steps require Internet access to install packages.

For the combined recorder and synthetic cameras:

```bat
.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-camera.txt
Start_Sync_Recorder.cmd --demo-cameras --demo-audio
```

`Setup_Windows.cmd --camera` performs the corresponding installation.
For recorder tests, use `Setup_Windows.cmd --dev`.

## Real microphones

Select the Ultramic input and connect at 200,000 samples/s, mono, 16-bit PCM.
The app supports WASAPI exclusive, WDM-KS, and direct Windows MME paths;
availability depends on the installed device and drivers. It does not silently
fall back to a lower sample rate. Close other applications using the microphone
if opening fails. `Check_Microphone.cmd` writes local diagnostics; do not commit them.

The microphone identifier field is optional. Enter your own label only if needed.
Outputs default to a folder under Documents; choose storage with enough free space.
Keep recordings outside your checkout and maintain a separate backup.

## Real Seek / FLIR cameras

1. **Camera Drivers**: The repository bundles both Seek WinUSB drivers (`drivers/Seek/seekusb.inf`) and FLIR USB3 drivers (`drivers/FLIR_USB3/PGRUSBCam3.inf`).
   - Right-click `Install_Camera_Drivers.cmd` and select **Run as administrator** once.
   - If Windows Device Manager still shows a warning on the Seek Thermal Camera, right-click it, choose **Update driver &rarr; Browse my computer for drivers**, and select the `drivers\Seek` directory. See [Camera Drivers Guide](CAMERA_DRIVERS.md).
2. **Seek SDK**: The Seek SDK library is bundled in `camera_sdk/seek/seekcamera.dll`. If using an external SDK installation, you can optionally override it via `set SYNC_SEEK_DLL=C:\path\to\seekcamera.dll`.
3. **FLIR Spinnaker / PySpin**: For Python source development, install the matching **Teledyne Spinnaker SDK** and its **vendor PySpin wheel** for Windows x64 / Python 3.12 (`.venv\Scripts\python.exe -m pip install "PATH_TO_VENDOR_WHEEL.whl"`). If using the exported standalone Windows bundle (`SyncRecorder.exe`), PySpin is pre-compiled and no external wheel installation is required.
4. Verify FLIR detection using SpinView or `Start_Sync_Recorder.cmd --check-setup`.

`Start_Sync_Recorder.cmd --check-setup` diagnoses installed libraries and enumerates devices. `--self-test` uses synthetic cameras and audio and needs no hardware.

## Calibration

Only **None (camera temperature)** is supplied. Keep it selected unless you have
validated a calibration for your setup. To register one locally, add a
`SeekCalibration(name, label, scale, offset_c)` entry to `SEEK_CALIBRATIONS` in
`ultramic_studio/recorder/cameras/calibration.py`. The conversion is `scale * camera_c + offset_c`.
Do not publish lab-specific names or coefficients unintentionally when sharing edits.

Calibration is locked while armed, recording, or finalizing. An unknown saved
calibration preference falls back to None. Take reports and thermal metadata record
the selected calibration. Camera color previews retain their vendor palette.

## Anaconda and optional executable bundles

An existing Conda environment can install the same requirements and launch
`python -m ultramic_studio` or `python launch_sync.py`.
`Start_Anaconda.cmd` supports the existing environment named `ultramic`.

If you separately build a Windows transfer bundle, keep its executable and complete
`_internal` directory together. Only that bundle uses `Setup_New_PC.cmd`.
See [local builds](DEVELOPMENT.md); no binary bundle is included in this repository.
