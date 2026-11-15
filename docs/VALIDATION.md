# Validation record

## Incremental recorder refactor — 2026-11-06

The configured Windows Python 3.12 suite passed **214 tests, with 2 skipped**.
One earlier full-suite attempt stopped with a native Qt access violation during
window construction; the subsequent complete run passed. Focused recorder, ROI,
camera SDK ABI, and UI checks also passed.

Synthetic recordings produced by the saved pre-refactor source and the new source
matched on 29 synchronization-report fields, five CSV files, and decoded Seek and
FLIR video frames. Cage exports matched on four CSV files, two thermal array
archives, and four decoded cropped videos. Exact PCM boundary tests also passed.
Recorder and cage UI captures were compared at 100%, 125%, and 150% scaling.

The rebuilt Windows portable package passed its isolated setup check and synthetic
recording/cage export self-test. No physical camera or microphone capture was tested.
The source snapshot, comparison script, screenshots, logs, and build are in
`.validation/` on this machine.

## Recorder and cage UI refresh — 2026-11-06

The local Python 3.12 test suite passed **208 tests, with 2 skipped**. Checks
include keyboard entry and validation of cage coordinates, persistent recording
and export status, accessible names, theme contrast, and checkbox rendering.
Synthetic recorder and cage-window screenshots were compared at 100%, 125%, and
150% Qt scaling; the compact cage window fit the 1320 × 690 capture area at all
three scales. Captures and pre-change source copies are under `.validation`.

A Windows portable executable built from the updated source passed the isolated
setup check and synthetic recorder/cage-export self-test; the checkbox SVG and
Qt SVG plugin were present in the bundle. These software checks did not use
physical cameras or a microphone, and they are not an accessibility certification.

## Recorder organization and cage export — 2026-11-06

The local recorder now keeps the cage selector and exporter under
`ultramic_studio/recorder/roi`, with root-level compatibility imports. The
local suite passed **201 tests, with 2 skipped** in the configured Python 3.12
environment. The eight previously recorded failures came from test fixtures and
expectations that predated the current camera readiness, segmented-video, and
camera-recovery behavior; the updated tests exercise those behaviors without
changing recording or recovery logic.

The separately reviewed public source passed **193 tests, with 2 skips** and
its Windows portable build passed setup plus synthetic recorder and cage-export
self-tests. Neither validation used physical cameras or a microphone.

## Recorder package refactor — 2026-11-05

The combined recorder implementation now lives in `ultramic_studio/recorder`;
see [Project structure](PROJECT_STRUCTURE.md). Validation used the configured
Windows Python 3.12.10 environment and synthetic cameras/audio.

- Before the refactor: **169 passed, 9 failed, 2 skipped**.
- After the refactor: **184 passed, 8 failed, 2 skipped**.
- All **17 startup/portable-source checks passed**, including the synthetic
  recording self-test, source relocation, compatibility entry points, and CMD
  argument/exit-code handling from another working directory.
- The self-test now decodes the Seek and FLIR video parts named in the take
  report, replacing obsolete assumptions about root-level MP4s and Seek PNGs.

The eight remaining failures also occurred before the refactor: three calibration
take-start cases, one audio-failure/output-file check, three shutter-failure
completion cases, and one post-click frame-retention case. Their details are in
`.validation/recorder-refactor-tests.txt`. Recording and recovery behavior in those
areas was not changed by the package migration.

Physical camera/microphone capture and a newly built frozen EXE were not tested
in this refactor. The portable source-copy path was tested in an isolated folder;
existing exported EXEs still need rebuilding to incorporate the changes.

## Combined recorder export — 8 October 2026

The full source suite passed **40 tests, with 2 platform/tool-dependent skips**
after adding portable setup. The new cases verify bundled Seek library lookup
after a folder move, explicit SDK overrides, and the synthetic recording check.

The Windows x64 EXE was checked with Python, camera SDK, GenTL, and Qt plugin
search paths removed from the environment. The bundled Seek and PySpin SDKs,
Qt, OpenCV, audio enumeration, and MP3 libraries loaded. Its synthetic self-test
rendered a Qt widget, wrote and decoded two FLIR MP4s, saved Seek PNGs, recorded
200 kHz mono PCM16 and separate pre-trigger history, produced the shared timing
report, and exported an MP3. A native-file audit checked **129 x64 DLL/PYD/CTI
files** and their non-Windows imports; unused x86/ARM64 PortAudio binaries were
excluded. The export includes the matching installed Seek and FLIR USB3 driver
packages and an administrator-only PnPUtil helper.

The driver helper was **not run** on this PC. Installation on a clean recording
PC, camera detection after driver installation, and physical synchronized
capture still require that PC's setup/bench checks. The build automatically runs
the software checks before creating its ZIP and writes `BUILD_INFO.json`, reports,
and a ZIP SHA-256 file. These checks do not establish compatibility with every
Windows installation or camera model.

## Combined Seek / FLIR / Ultramic recorder — 8 October 2026

The final suite passed **37 tests, with 2 skipped**, on this Windows PC using
the project's Python 3.12.10 environment. The skipped checks require FFmpeg
for independent MP3 decoding and a POSIX pseudo-terminal for serial integration.
The standalone audio tests still pass alongside the new shared-recorder tests.

| Area | Evidence |
| --- | --- |
| Shared start gate | Cached Seek/FLIR frames are rejected; the first post-click Seek callback is retained even if it arrives while the GUI reads settings. Both Seek sampling modes save the reference frame. |
| Audio boundaries | Deterministic PCM comparisons verify an in-block start, pending start/stop, retroactive WAV trimming, separate pre-trigger history, and contiguous WAV segments. |
| Driver timestamp fallback | Invalid future ADC times and batched callback timestamps preserve a contiguous nominal sample timeline. Expired audio history produces an error instead of silently selecting a later start. |
| Camera-only mode | Real loopback START/STOP/MARK/STATUS commands exercise explicit arming, duplicate START handling, and takes that create no microphone output. |
| Failures | A simulated microphone disconnect flags the take, drains camera files, and permits a subsequent camera-only take. An unavailable output folder fails without leaving the recorder stuck. |
| Combined GUI | Offscreen Qt exercised camera-only recording, demo microphone connection, shared audio takes, frequency controls/freeze, automatic MP3 export, WAV review, and graceful close. Multiple completed takes queued while Qt is busy retain their own reports and exports. |
| GUI layout | Synthetic camera and spectrogram screenshots were inspected at the default size and at 1050 × 700. Record/Stop stay in the top row when settings require scrolling. Windows fonts were loaded explicitly for the offscreen screenshot harness. |
| Camera SDK loading | The Seek SDK DLL and installed Spinnaker/PySpin 4.4.0.246 import successfully in the project's environment. The matching installed vendor package was made available to this `.venv`; no unrelated PyPI `pyspin` package was used. |
| Physical Ultramic capture | The actual `UltraMic 200K 16 bit r4` WASAPI input completed a shared GUI take with simulated cameras. WAV format was 200,000 Hz, mono, 16-bit, with 399,863 main samples, separate available pre-trigger history, normal completion, and no detected capture errors. |

In the tested setup, WASAPI's reported ADC timestamps were later than its callback
timestamps. The recorder therefore labelled the saved audio timing
`driver_latency_estimate`, using a nominal sample timeline anchored with the
reported 13.12 ms input latency. Its reported 1,000 ns first-sample offset was
rounding within that estimate, **not an independently measured physical offset**.
The final real-microphone GUI check follows the fallback-continuity correction.
MME format query succeeded, but opening the input failed with Windows MME error
11 (invalid parameter); WASAPI is the tested input path on this PC.

No physical FLIR camera was detected. The Seek DLL loaded, but the streaming
probe did not find a Seek camera delivering frames. Consequently, simultaneous
physical Seek/FLIR/microphone acquisition has **not** been verified. Camera
start offsets, disconnect behavior, and recordings were exercised with labelled
synthetic cameras. Hardware exposure alignment, ultrasonic frequency response,
long-run clock drift, full USB load, and a physical TTL bridge remain bench checks.

GUI images are in `docs/images/unified_cameras.png` and
`docs/images/unified_spectrogram.png`. Local bench recordings and probe results
are in `.validation/`; they are test artifacts rather than experiment recordings.
See [SYNCHRONIZATION.md](SYNCHRONIZATION.md) for interpreting the reports and
planning the connected-camera bench check.

## Frequency controls and portable distribution (0.1.2) — 6 October 2026

All **28 existing tests passed** after the frequency-view update. An additional
offscreen GUI check exercised typed 50–100 and 80–100 kHz ranges, changes while
frozen, preservation across live updates, two-way synchronization with plot zoom,
crossing bounds, the Full range button, saved preferences after reopening,
recorded WAV review, and the 22.05 kHz upper limit of a 44.1 kHz WAV. Controls
were checked at the minimum window width, and new demo screenshots were captured.

The portable package contains the official Windows x64 embedded Python runtime
and Windows wheels for all dependencies. The runtime download is checked against
the SHA-256 published on its official release page. Package versions and hashes
are recorded in `docs/PORTABLE_BUILD.json`; dependency licenses remain included.
A static dependency check traversed 51 bundled Windows x64 binary dependencies
used by the app, including the Qt Windows platform plugin, NumPy, MP3 encoder,
and PortAudio. Python, Qt, MSVC, and numerical-library imports resolved within
the package; operating-system DLLs are supplied by Windows. This is a file and
architecture check, not a Windows execution test.
The Windows runtime and launch scripts have not been executed here. These
packaging checks do not establish compatibility with every Windows installation.

## MME update (0.1.1)

The full suite passed **28 tests** on Linux / Python 3.12.14 with Qt offscreen.
The eight added cases use a simulated WinMM driver; they are not physical
microphone or Windows integration tests. They cover:

- Exact 200 kHz mono PCM16 format requests, with `WAVE_FORMAT_DIRECT` set for
  both the support query and actual open; an unsupported format gets no fallback.
- Actual WinMM IDs rather than PortAudio enumeration positions, including the
  difference caused by PortAudio's default mapper entry.
- Ring-buffer reuse across 75 blocks, sample-for-sample WAV preservation, a
  partial final buffer, and absence of invented ADC timestamps.
- Buffer exhaustion, invalid/short buffers, failed startup cleanup, and
  Windows structure sizes.

Windows execution of the new Anaconda launcher and the direct WinMM capture
path has not been performed here. The microphone needs a connection and
ultrasonic signal check on the user's Windows PC. MME does not provide the
same timestamp or overrun reporting as all other backends; see the README.

## Original release checks (28 September 2026)

**20 tests passed** on Linux x86_64 / Python 3.12.14. The suite completed in
1.36 seconds for the final run. Qt tests used the offscreen platform; the
application was also run for a longer synthetic demo and its live/review
screenshots were visually inspected. Python source compiled successfully.

| Area | Evidence |
| --- | --- |
| Raw data preservation | Random 16-bit PCM was compared byte for byte after writing and reading WAV, including a pre-trigger boundary that did not align to an input block |
| File segmentation | Concatenated WAV segments matched input PCM without missing or duplicated samples |
| Spectrogram | A 62.5 kHz sine wave was localized to the correct FFT bin; a half-scale signal measured approximately −6.02 dBFS |
| Streaming display | FFT frames remained continuous across irregular input chunks; bounded display pooling retained the test tone |
| TCP triggers | Actual loopback socket commands tested arming, START/STOP/MARK, duplicate commands, and simultaneous START requests |
| Serial triggers | Actual OS pseudo-terminal byte streams tested fragmented serial JSON and start/stop delivery with device timestamps |
| MP3 output | FFmpeg independently decoded the export: a 60 kHz source tone appeared at 6 kHz; a 0.2 s source produced approximately 2 s of MP3 audio plus encoder padding |
| Failure handling | Recording queue saturation, sample discontinuity, simulated driver overflow, low disk space, and encoder failure were exercised |
| Original preservation | Failed MP3 encoding left the WAV unchanged and removed the interrupted export's temporary file |
| Native rate rejection | A mocked driver reporting 48 kHz was refused; this was a software guard test, not a real device test |
| GUI workflow | Demo connection → armed external recording → stop → background MP3 → WAV review completed successfully |
| Dependencies | Pip resolved all pinned requirements to Windows x64 / Python 3.12 wheels in a dry run |

Tests are in `tests/`. The MP3 decode test requires FFmpeg; the normal application
does not. The pseudo-terminal test is specific to POSIX and is skipped on Windows.

## Not executed here

The exclusions in this section refer to the original standalone release checks.
The combined recorder's subsequent Windows/microphone checks are recorded above.

- Windows execution of the setup scripts, microphone capture, or the optional
  PyInstaller build.
- Physical capture from a Dodotronic unit with the microphone serial entered by
  the operator; the serial is a supplied label, not a device identity verified
  by the software.
- Arduino compilation/upload, electrical TTL testing, or latency measurement.
- MATLAB client execution.
- Long-duration capture on the recording PC, power-loss recovery, clock drift
  characterization, acoustic calibration, or a comparison against Avisoft.

## First bench check on the actual system

1. Run the synthetic demo and save a short session. Open the WAV and listening MP3.
2. Connect the Ultramic at native 200 kHz. Record a known ultrasonic source and
   check its frequency. A standard PC speaker is not a known 60 kHz source.
3. Send one hardware gate (or separate start/stop pulses). Verify one recording
   folder, the START/STOP events, and the amount of pre-trigger history.
4. Send START/STOP from the experiment program and check the response and files.
5. Run a representative-duration capture using the actual USB connection and
   destination disk. Check `session.json` for normal completion and assess the
   audio before using the setup for experimental data.
6. If precise alignment with behavior, camera frames, or neural data is required,
   independently measure the audio/trigger relationship and drift. Host serial
   timestamps alone do not establish that alignment.

The software tests support the behavior of the implementation. They do not
establish physical microphone compatibility, real-world timing accuracy, or
experimental data quality.
