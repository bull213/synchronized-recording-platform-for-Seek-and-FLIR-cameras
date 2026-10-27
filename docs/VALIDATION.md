# Source validation

## Public source preparation — 27 October 2026

Validated on Windows x64 with Python 3.12.10 in a freshly created virtual
environment using only requirements-dev.txt. A separate checkout was populated
from the reviewed source inventory. Python, Qt, and SDK search-path overrides
were cleared; vendor PySpin and Seek SDKs were absent.

| Check | Result |
| --- | --- |
| Dependency installation | All pinned requirements installed; pip check reported no conflicts |
| Recorder and publication tests | **121 passed, 2 skipped** |
| Synthetic recording self-test | **Passed** |
| Source inventory | **89 reviewed files**, including hidden Git configuration |
| Documentation screenshots | Four fresh captures of the actual GUI with synthetic inputs and generic settings |

The two skips are explicit: the POSIX pseudo-terminal serial test on Windows,
and independent MP3 decoding without FFmpeg on PATH. MP3 encoding and file creation
were exercised by the application tests and self-test.

The suite covers original PCM preservation, pre-trigger and shared sample boundaries,
trigger arming, GUI workflows, resource cleanup, synthetic calibration, segmented camera
files, recovery warnings/gaps, and source archive boundaries. The self-test renders Qt,
writes and decodes segmented Seek and two FLIR MP4 streams, saves thermal CSV,
records 200 kHz mono PCM16 WAV with separate pre-trigger history, writes timing
reports, and exports MP3. No physical acquisition was performed.

The previously failing checks used obsolete video paths, incomplete simulated
camera state, or immediate-stop expectations predating camera recovery. Their
fixtures and expectations were updated for current behavior. The publication
cleanup leaves acquisition, recovery policy, and recording schema unchanged.

The Windows GitHub Actions workflow is included but has not run on GitHub yet.
The local source checks do not validate portable executables, driver installers,
other Python versions, or every Windows/device configuration.

## Physical hardware

This source preparation does not establish physical Seek/FLIR/microphone timing,
device compatibility, full USB throughput, long-duration drift, or TTL electrical
behavior. Synthetic tests and mocked SDK interfaces cannot establish those results.

Before collecting experimental data:

1. Verify camera detection and native 200 kHz microphone capture on the recording PC.
2. Record a known reference and inspect raw WAV / thermal CSV and take reports.
3. Exercise Record, Stop, external triggers, and device disconnect/recovery.
4. Run a representative-duration test under expected USB and disk load.
5. Measure physical alignment and clock drift with a common reference when needed.
6. Check backups and recovery from interrupted recordings.

See [synchronization](SYNCHRONIZATION.md) for interpreting saved timestamps,
segments, dropped frames, and gap reports.
