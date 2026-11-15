# Ultramic Studio 0.1.2

The combined recorder has a refreshed dark layout, persistent recording status,
expandable settings, and optional live audio beneath the cameras. See the
[recorder interface guide](docs/RECORDER_GUI.md).

A Windows-oriented desktop recorder for the Dodotronic Ultramic 200K.
Enter the microphone serial number in the app when needed. Includes full Python source, Windows
launch scripts, a synthetic demo, and optional Arduino TTL bridge firmware.
The portable Windows package includes Python and the standalone microphone app dependencies.

**For the combined Seek / FLIR / microphone recorder, use
[`Start_Sync_Recorder.cmd`](Start_Sync_Recorder.cmd).** In an exported folder it
opens the bundled Windows EXE; in the editable project it uses `.venv`.
The standalone microphone portable runtime is a separate launcher.

**Move the combined app to another PC:** double-click
[`Export_Sync_Recorder.cmd`](Export_Sync_Recorder.cmd) on the development PC.
Copy the resulting ZIP from `exports`, extract it on the new Windows x64 PC,
and run **`Setup_New_PC.cmd`**, then **`Start_Sync_Recorder.cmd`**.
Python and the app libraries are included. See
[`START_SYNC_HERE.html`](START_SYNC_HERE.html) for the short setup guide.

**Start with `START_HERE.html` for the illustrated setup guide.**

The original standalone release was tested with synthetic audio. The physical
microphone, Windows audio drivers, and Arduino hardware were not available in the
original development environment. Current combined-recorder checks are listed
below and in [docs/VALIDATION.md](docs/VALIDATION.md). Verify the full setup before collecting
experimental data. This is a portable folder with the app's Python source and
Windows runtime; it is not a signed application installer.

## Unified Seek / FLIR / microphone recorder

The recorder opens one Qt GUI with **Cameras**, **Live spectrogram**,
**Review recording**, and **Triggers** tabs. Its implementation lives in
`ultramic_studio/recorder`, with separate camera, recording, and UI modules.
`launch_sync.py` and `sync-rec.py` remain supported launchers. See the
[project structure and development guide](docs/PROJECT_STRUCTURE.md).

1. Double-click **`Start_Sync_Recorder.cmd`**. The cameras connect in the background.
   In the editable project, `.venv\Scripts\python.exe launch_sync.py` also works.
2. Select **Cameras only** to record Seek and all connected FLIRs with no microphone.
   This is the default. No audio input is opened or audio files created.
3. For audio, select **Cameras + microphone**, choose a supported Ultramic input,
   and click **Connect & monitor** before Record.
4. Choose a recording folder and label, set Seek sampling and shutter options,
   and click **Record**. **Stop** finishes the shared take. Repeated takes share
   a session folder and have separate FLIR videos, audio folders, and timing reports.
   Restart the app to begin a new session folder.

**Separate cage thermal data:** double-click **Start_Cage_ROI.cmd** to draw a
rectangle around each cage in a finished recording and export per-mouse cropped
pixel arrays, temperature CSVs, and optional Seek video parts. In the combined
recorder, open **View → Cage ROI export…**. See [Cage ROI instructions](docs/CAGE_ROI.md).

Select **Seek only** to record with just the Seek camera, without FLIR or a
microphone. This saves Seek MP4 video, thermal CSV data, and timing reports.
All Seek sampling and calibration options remain available; connected FLIRs
are excluded from the recording. Switching recording modes starts a new session folder.

### Example Seek output

The image below shows an example thermal frame from the Seek output pipeline.

![Example thermal frame from the Seek output pipeline](docs/images/seek-example-output.png)

For independent media-only recording, select **FLIR only (.mp4)** or
**Microphone only (.mp3)**. FLIR mode saves one MP4 per connected FLIR camera
without requiring Seek or a microphone. Microphone mode requires only a connected
microphone and saves a normal-speed MP3 after Stop, without time expansion.
Successful takes in these modes contain only the selected media files; temporary
audio is removed after conversion succeeds and retained for recovery if conversion
fails. Switching between these modes and the combined modes starts a new session
folder. Drag the dividers between camera preview panels to resize them.

**Laptop layout:** the combined recorder sizes itself to the available screen.
Use **Hide settings** or drag the sidebar divider to give the cameras more room.
**Preview mode** hides extra panels while keeping Record, Stop, recording mode,
status, and duration accessible. Click **Enlarge** below a camera or double-click
its image; **All cameras** returns to the grid. Escape returns to all cameras
first, then exits Preview mode. **View** contains the camera-options and message-log
toggles, session/export actions, and **Reset layout**. Panel visibility and sidebar
width are remembered locally; each launch starts with all cameras visible.

For **After shutter request** sampling, select **Custom** shuttering, set its rate,
and click **Apply shutter** before Record. The app issues a shutter request
immediately on Record, then saves one valid frame received after each successful
shutter call. The shutter rate controls saving; the separate sampling rate is
disabled. Live previews continue streaming. Images go into `seek_after_shutter`,
and thermal metadata and take reports include request IDs and request/return/frame
receipt times. One request may be pending at a time; timer ticks while waiting are
skipped. An SDK failure or five seconds without a valid frame ends the take with
an error. Stop cancels pending selection. This timing does **not** confirm that the
physical shutter has reopened: the SDK call requests shuttering as soon as possible.

**Room calibration:** in **Seek camera options → Calibration used**, choose
**None (camera temperature)** (the default) or **11-computer-room**. The latter
converts each Seek temperature in Celsius using
`true_c = 0.732150057409 * camera_c + 7.599400024682` before saving it to
`seek_thermal.csv`. Temperatures retain the CSV's one-decimal formatting. The
selection is remembered locally and can be changed between takes; it is locked
while triggers are armed or a take is recording/finalizing. Each take's sync
report and thermal frame metadata record the calibration name, coefficients,
and Celsius units; `seek_log.csv` also identifies the calibration for each frame.
The color preview and PNGs continue to use the camera's palette. To add another
room, register its coefficients in `ultramic_studio/recorder/cameras/calibration.py`.

In the two combined camera modes, Record sets a pending request. The **first fresh valid Seek frame after the
request** supplies the common host-monotonic reference, T0. The first Seek frame
is saved in the custom-rate and every-frame modes. In After shutter request mode,
the first frame selected after the first shutter request supplies T0 instead.
Each FLIR saves frames received at or after T0.
The microphone monitors continuously and keeps bounded history, allowing its
unmodified PCM to be trimmed to the first estimated sample at or after T0,
including a boundary inside an audio block. Stop trims audio at the shared stop
reference. Any requested **pre-trigger audio is a separate `pretrigger.wav`**;
it does not shift the main WAV's sample zero.

All standalone microphone features remain available: device selection, native
200 kHz capture, synthetic audio, serial metadata, live/review frequency ranges,
FFT size, display floor/history, freeze, zoom/pan/cursor, peak/clipping display,
WAV review, 30-minute WAV splitting, and optional 10× MP3 export/playback.
Serial and localhost TCP triggers control the entire selected take, including
camera-only mode, and require explicit arming. Their START response may report
`pending`; STATUS reports when the Seek reference has been established.

Each take saves **`take01_sync.json`** (then `take02_sync.json`, etc.), including
the requested time, Seek T0, stop time, first-stream offsets, frame counts/gaps,
audio timestamp quality, and completion/error status. Camera CSVs and
`microphone/take01/audio_timing.csv` provide per-frame/block details.
See **[docs/SYNCHRONIZATION.md](docs/SYNCHRONIZATION.md)** for file layout and checks.

**This is software alignment using receipt times and labelled audio estimates.**
The free-running FLIRs cannot acquire a frame at an arbitrary Seek instant.
A sub-5-microsecond audio offset in the report is sample-grid rounding of the
estimate, not measured physical accuracy. USB latency and independent device
clocks require a common measured reference to establish physical simultaneity.

The exported app includes its own Python, OpenCV, Qt/audio libraries, matching
Spinnaker/PySpin runtime, and Seek SDK DLL. It loads the bundled Seek DLL relative
to the app folder. You can extract the ZIP to a different path on another PC.
No Python, Anaconda, pip, or Internet connection is needed to run that app.
Keep its complete `_internal` folder beside `SyncRecorder.exe`.

The export also includes the installed vendor Seek WinUSB and FLIR USB3 driver
packages. If cameras are not detected on the new PC, right-click
**`Install_Camera_Drivers.cmd` → Run as administrator** once, then reconnect them.
This helper adds matching drivers with Windows PnPUtil; it does not delete drivers
or force a lower-ranked driver. For GigE or other camera models, use the appropriate
vendor SDK/driver installer. SDKs and drivers retain their supplied vendor terms.

For editing/rebuilding, use Python 3.12 and the Spinnaker/PySpin package version
matching your FLIR SDK. `Export_Sync_Recorder.cmd` installs pinned build
requirements if needed and creates a fresh ZIP without replacing earlier exports.
Each export includes editable app source, docs/licenses, checksums, build information,
and software-check reports. Recordings, `.venv`, IDE files, and local bench artifacts
are excluded. Changing source requires another export to update the EXE.

If setting up a separate development environment instead of using the ready-built
app, install `requirements-camera.txt` and the **vendor Spinnaker Python wheel**
matching Python and Windows architecture. The unrelated PyPI `pyspin` is not the
FLIR SDK. Set `SYNC_SEEK_DLL` to a licensed SDK `seekcamera.dll` if it is not in
the app folder. The existing standalone launchers still open microphone-only mode.

To try the whole GUI without camera or microphone hardware:

```bat
Start_Sync_Recorder.cmd --demo-cameras --demo-audio
```

Demo previews, folder names, and metadata are labelled **DEMO**. Start/stop is
still manual. Omit `--demo-audio` to test camera-only mode or connect a real mic
with simulated cameras. [Camera GUI preview](docs/images/unified_cameras.png) ·
[Spectrogram GUI preview](docs/images/unified_spectrogram.png).

## What it does

- Opens a mono 16-bit input at **200,000 samples/s**. Windows MME is supported
  through a direct WinMM capture path with ACM format conversion disabled;
  WASAPI exclusive mode and WDM-KS are also available. No lower-rate retry.
  The serial number is metadata, not an automatically verified USB ID.
- Shows a scrolling spectrogram with editable **Frequency Min / Max** controls
  in both the live and review views. Choose, for example, **50–100 kHz** or
  **80–100 kHz**. Also includes selectable FFT size, display floor, history
  window, cursor coordinates, and input level.
- Records manually or via **TTL-to-USB serial** and **localhost TCP** commands.
  Both sources can be enabled. External commands require explicit arming in the app.
- Saves the unmodified PCM to WAV, optionally includes up to 5 seconds of
  pre-trigger audio, and splits long recordings into contiguous 30-minute files.
- Creates an optional **10× time-expanded MP3** after each recording. A 60 kHz
  call becomes 6 kHz; a 1-minute recording becomes a 10-minute listening file.
- Opens saved mono 16-bit WAV recordings for spectrogram review and MP3 export.
- Logs start/stop/marker events, sample positions, device information, and driver
  timestamps where available. MME has host receipt times, without ADC timestamps.
  Stops and flags incomplete data after detected overflow or capture failure.

At 200 kHz sampling, the Nyquist limit is **100 kHz**; vocalization components above
that cannot be faithfully captured. That is a hardware limit. MP3 cannot retain
the original ultrasound: **use WAV for scientific analysis**. MP3 is lossy and is
provided only as an audible listening copy. No automatic call classification or
syllable detection is included in this version.

## Windows installation

### Portable package: no Anaconda or Python installation

1. Extract the entire ZIP into a folder on a **64-bit Windows 10 or 11 PC**.
   Do not run the app from inside the ZIP.
2. Open the extracted `IR-project` folder and double-click
   **`Start_Ultramic_Studio.cmd`**. No setup command or package download is needed.
3. Confirm **0.1.2** appears in the window title. Select the Ultramic's **MME**
   entry and click **Connect & monitor**.

Keep the whole folder together, including `runtime`. You can copy the folder
to another Windows x64 computer. The app uses its bundled Python and libraries;
an existing Anaconda installation is optional. Windows ARM64, macOS, and Linux
are not targets of this portable binary package.

### Adjust the frequency window

Above either spectrogram, enter **Min** and **Max** in kHz and press **Enter**.
For a 50–100 kHz view, set Min to `50` and Max to `100`. Use **Full range** to
show the entire available band. The controls also work with **Freeze display**
enabled, and follow vertical mouse zoom/pan. Each view remembers its range.

For live Ultramic 200K input, Max is capped at **100 kHz**. For a loaded WAV,
the limit follows half that file's sample rate. A display change does not filter
or crop the recorded audio. The **Window** control adjusts time, independently.

### Already running with Anaconda? Update without reinstalling

1. Close the app and Avisoft.
2. Extract this updated ZIP. Open the extracted `IR-project` folder.
3. Double-click **`Start_Anaconda.cmd`**. It uses the existing Conda environment
   named `ultramic`; no new packages or Python launcher are needed for this update.
4. Confirm **0.1.2** appears in the window title. Select
   **Ultramic 200K 16 bi [MME]**, then **Connect & monitor**.
   Live viewing does not save audio; click **Record** when you want a file.

The launcher checks the usual Anaconda/Miniconda locations. If yours is elsewhere,
open Anaconda Prompt and run the following, replacing the quoted path:

```bat
conda activate ultramic
cd /d "C:\path\to\the\extracted\UltramicStudio"
python -m ultramic_studio
```

For a first Anaconda installation, create the environment once with
`conda create -n ultramic python=3.12 pip -y`, activate it, change to the extracted
folder, and run `python -m pip install -r requirements.txt`.

### Standard Python installation

1. Extract the entire ZIP into a writable folder, such as
   `Documents\UltramicStudio`. Do not run scripts from inside the ZIP.
2. Install **64-bit Python 3.12** from
   [python.org](https://www.python.org/downloads/windows/), including the Python
   launcher. Python 3.11 and 3.13 are also accepted by the setup script.
3. Double-click **`Setup_Windows.cmd`**. It creates a private `.venv` and downloads
   pinned dependencies. This one-time setup needs an Internet connection.
4. Double-click **`Start_Ultramic_Studio.cmd`**. The console remains open so errors
   can be read. After setup, recording and export work offline.

The dependency resolver was checked for Python 3.12 / Windows x64 wheels. The
scripts themselves have not been executed on Windows in this environment.

Optional: `Build_Windows_App.cmd` builds an executable on your Windows PC using
PyInstaller. Keep the whole `dist\UltramicStudio` folder together. This optional
build path is supplied for convenience and has not been run or hardware-tested.

## First run: try the demo

1. Choose **Demo · synthetic calls**, choose a recording folder, and click
   **Connect & monitor**.
2. Click **Record**, wait a few seconds, then **Stop**.
3. Click **Open last session** to see the WAV files and metadata. If MP3 is enabled,
   wait for “Audible MP3 ready,” then click **Play last MP3**.
4. Under **Review recording**, open the saved WAV. Set a start time and a view
   duration; click **Load window**. Shorter windows give more detailed displays.

Demo recordings are explicitly labelled `DEMO` in their folder names and metadata.
They contain computer-generated chirps, not real mouse vocalizations.

## Connect the Ultramic

1. Disconnect demo mode. Plug the microphone into USB and click **Refresh audio devices**.
2. Choose **Microphone · 200 kHz**, then **Ultramic 200K 16 bi [MME]** if that is
   the available entry. **Windows WASAPI** or **Windows WDM-KS** entries can also
   be used. Windows may expose the same device through several APIs.
3. Close Avisoft and other programs using the microphone. Click **Connect & monitor**.
4. Check that the display contains ultrasonic energy and that peaks do not clip.
   Hardware gain and microphone placement determine acquisition gain; display
   contrast does not change the recording.
5. Record a short known ultrasonic signal and inspect the result. A reported
   200 kHz stream alone cannot prove that the complete hardware/driver path is
   preserving ultrasound. Do this check before relying on recordings.

If it fails to connect, run **`Check_Microphone.cmd`** with the app disconnected.
It lists audio devices and briefly probes named Ultramic inputs at 200 kHz,
without saving sound. Results go into `microphone_diagnostics.txt`.
If the device has a generic name, use its listed index explicitly:

```bat
.venv\Scripts\python.exe -m ultramic_studio.diagnostics --device 4
```

If none of these capture paths supports 200 kHz for this specific unit, use
the diagnostic report to resolve the driver/device path. This version does not
include a custom Dodotronic USB driver or silently substitute a lower sample rate.

The MME path uses the actual WinMM device ID rather than guessing from a
PortAudio index. It asks Windows to disable ACM conversion using
[`WAVE_FORMAT_DIRECT`](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveinopen).
Driver acceptance does not independently prove hardware sampling accuracy or
ultrasonic frequency response. Its 32 input buffers hold about 328 ms in total;
this is queue capacity, not a measured input latency. Buffer starvation and a
three-second input stall stop capture, but MME cannot report every possible
driver or USB loss. Trigger boundaries remain based on audio delivered to the app.

## Triggers

See **[docs/TRIGGERS.md](docs/TRIGGERS.md)** for wiring and full protocol details.

**Software:** Enable software trigger, then arm triggers. Another program on the
same computer connects to `127.0.0.1:8765` and sends one line, such as:

```json
{"command":"START","label":"mouse_01_trial_02"}
```

Send `STOP` to finish, `MARK` to insert a labelled event, or `STATUS` to query the
recorder. Each connection receives one JSON response. Python and MATLAB examples
are included in `examples/`. Only the app can arm or disarm triggers.

**TTL:** The UM200K documentation describes USB audio and does not specify a TTL
input. Use an external digital-input bridge. The supplied Arduino Uno R3/classic
Nano firmware supports either a high/low gate on D2 or separate start and stop
pulse inputs on D2/D3. Connect its COM port at 115200 baud, wait for READY, then arm.
An ordinary USB-to-RS-232 cable is not this bridge.

Disarming also stops the current recording. The **Stop** button stops the current
file but leaves triggers armed. Disable/disconnect a trigger connection to disarm
and stop. Both enabled sources control the same recording; STOP from either ends it.

## Files and timing

Each recording creates its own uniquely named folder:

| File | Contents |
| --- | --- |
| `audio_0001.wav`, `audio_0002.wav`, … | Original 200 kHz / mono / 16-bit PCM, consecutive segments |
| `session.json` | Settings, user-entered microphone serial, file/sample map, status, errors |
| `events.jsonl` | START/STOP/MARK events with host receipt timestamps and sample boundaries |
| `audio_blocks.csv` | Stream/file sample positions and host receipt times; ADC fields are blank with MME |
| `listening_10x.mp3` | Optional slowed audible version of the complete session |
| `listening_10x.mp3.json` | Source information and frequency/time transformation |

WAV storage is approximately **24 MB/minute or 1.44 GB/hour**, plus small metadata
files. A 30-minute segment contains 360 million samples and about 720 MB of PCM.
Write to a local disk during capture; copy recordings to your NAS afterward.

The 0.5-second default pre-trigger history is available only after the microphone
has been monitoring that long. `pretrigger_samples` records the actual amount.
The START event's `file_sample` is the boundary after that history. Consecutive
trials can intentionally share pre-trigger samples.

**USB triggers are asynchronous.** Commands take effect between queued audio
blocks; the requested block size is 2048 samples (10.24 ms). Driver latency, USB
serial delivery, operating-system scheduling, and writer queues add variable
delay. The 10.24 ms value is not a timing accuracy guarantee. The Arduino's
`device_us` clock is independent of the audio clock, resets on reboot, and wraps
after about 71.6 minutes. It is logged as received, not converted to audio time.
For tightly synchronized neural/behavioral alignment, an independently measured
common timing reference is still needed.

Amplitude is displayed in **dBFS**, not calibrated acoustic SPL. FFT uses a Hann
window and 75% overlap. Long views use maximum pooling across adjacent time bins
for display. It affects only the image. To examine a short call in detail, load a
shorter review window rather than merely enlarging a pooled image.

## Integrity and recovery

Audio writing runs separately from the GUI. If the preview falls behind, preview
frames can be skipped while the recording continues. If the **recording** queue
overflows, the audio driver reports a problem, the device disconnects, or less than
100 MB remains on disk, capture stops and the session is flagged as incomplete
where storage is still writable. An armed bridge failure also stops capture.

Headers are updated with each write and metadata periodically. This improves
recoverability but does not guarantee recovery after a power failure or failing
disk. Inspect `session.json`: `complete` means a normal close, `error` marks a
detected acquisition problem, and `recording` after a crash means closure was not
confirmed. Do not assume a stale “recording” session is complete.

MP3 export runs after recording, in the background. An export failure leaves the
WAV files intact. Very short start/stop commands may produce a session with no new
audio blocks; any available pre-trigger history is still retained. Empty sessions
cannot create an MP3. Keep the app open until queued exports finish.

## Validation and development

See [docs/VALIDATION.md](docs/VALIDATION.md). To run the included checks:

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
```

Independent MP3 decode verification uses FFmpeg if available; normal app export
does not require FFmpeg. The serial pseudo-terminal integration test runs on
POSIX. The Windows PC and physical TTL bridge still need their own end-to-end test.

## Primary references

- [Dodotronic UM200K and user guide](https://www.dodotronic.com/product/ultramic-um200k/)
- [Dodotronic manual: model-specific sampling and bit depth](https://www.dodotronic.com/wp-content/uploads/2022/11/Ultramic_User_Guide-1.pdf)
- [sounddevice native platform settings](https://python-sounddevice.readthedocs.io/en/latest/api/platform-specific-settings.html)
- [pySerial](https://pyserial.readthedocs.io/en/latest/pyserial_api.html)
- [LAME Python encoder](https://github.com/chrisstaite/lameenc)
- [Arduino interrupts](https://docs.arduino.cc/language-reference/en/functions/external-interrupts/attachInterrupt/)
- [PyInstaller usage](https://pyinstaller.org/en/stable/usage.html)

Third-party libraries retain their own licenses. They are downloaded during
setup; their binaries are not bundled in this source archive.
