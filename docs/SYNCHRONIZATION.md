# Combined recorder: synchronization and saved files

Use `Start_Sync_Recorder.cmd` for the combined app. The default **Cameras only**
mode requires Seek and at least one FLIR, but no microphone. Select
**Cameras + microphone** and connect/monitor the microphone before recording.
Microphone connection failures leave camera-only mode available. A selected
microphone failure during a take stops and flags that take; select Cameras only
after finalization to continue recording cameras.

## What establishes the start

1. A manual or armed external START records `request_monotonic_ns` using the
   same Windows/Python performance-counter clock as camera callback timestamps.
2. The earliest fresh Seek callback with valid thermal pixels and metadata at
   or after the request sets `shared_start_monotonic_ns`, called T0. A small
   recent-frame buffer covers the interval while the GUI reads its controls.
   An older cached frame cannot establish the start. If Seek is unavailable, the
   take remains pending while the watchdog reports recovery attempts; STOP cancels
   a take that has not established T0. Other streams cannot establish T0 in this mode.
3. Seek saves this frame even in custom-rate mode. Custom sampling then chooses
   fresh callbacks on the requested schedule; every-frame mode saves each callback.
4. FLIRs run continuously. Frames received before T0 are excluded; each first
   saved frame's measured receipt offset is reported. At 30 fps, a first frame
   can arrive roughly one frame period after T0, with additional scheduling/USB delay.
5. The already-monitoring microphone retains about ten seconds of PCM history.
   The writer finds T0 in the estimated sample timeline and selects the first
   sample at or after it. It can recover samples already delivered before the
   Seek callback reaches the controller. PCM values are not resampled or filtered.

The sample interval at 200 kHz is 5 microseconds. Rounding a start to the next
sample usually yields an estimated offset from zero up to 4,999 ns. This only
describes sample selection within the chosen timeline.

STOP records T1 on the common host clock and closes audio on the half-open
interval `[T0, T1)`. If the block containing T1 was already written, the writer
trims the WAV and sample logs before marking the take complete. Camera queues
drain and FLIR MP4 files close before another take can begin. A START cancelled
before any valid Seek frame has no microphone recording.

## Timestamp sources and their limits

| `timestamp_source` | Meaning |
| --- | --- |
| `host_frame_receipt` | Seek callback entry or FLIR `GetNextImage` return time. This is not exposure time. |
| `driver_capture_estimate` | Plausible driver ADC time mapped to the host performance-counter clock. |
| `driver_latency_estimate` | ADC time unavailable/invalid; nominal sample timeline anchored with callback time minus reported input latency. |
| `host_receipt_estimate_latency_unknown` | MME receipt-based nominal sample timeline; input latency is unknown. |
| `synthetic` | Computer-generated demo audio, with no physical microphone. |

For PortAudio capture, clock reads are bracketed by host-counter reads outside
the audio callback. The narrowest bracket supplies the mapping; anchors are
logged and refreshed during GUI monitoring. `clock_bracket_width_ns` measures
the host clock-read bracket, **not** device latency or total timing uncertainty.
The last 120 anchors are retained, and every written block records its estimated
time, source, bracket, driver times, and host receipt time.

Latency/receipt fallback timelines advance at the nominal 200 kHz rate, so
batched callbacks cannot give consecutive blocks overlapping estimated times.
Their long-term device-clock drift is not corrected. Raw camera SDK timestamps
are also retained, but belong to independent clocks and must not be directly
compared with audio ADC times or host-monotonic timestamps.

Software receipt timestamps cannot prove simultaneous acquisition. Establish
physical alignment and drift using a reference observable by each device, or
appropriate hardware synchronization. The GUI displays measured first receipt
offsets and labels the microphone offset as an estimate.

## Files

One session directory holds repeated numbered takes:

```text
session_YYYYMMDD_HHMMSS/
  take01_sync.json
  take01_events.jsonl
  take02_sync.json
  take02_events.jsonl
  flir_log_cam0.csv
  flir_log_cam1.csv
  seek_log.csv
  seek_thermal.csv
  video_parts/
    seek_take01_part0001.mp4
    seek_take01_part0001.csv
    flir_cam0_take01_part0001.mp4
    flir_cam0_take01_part0001.csv
    flir_cam1_take01_part0001.mp4
    flir_cam1_take01_part0001.csv
    ...                              # more parts / takes as needed
  microphone/
    take01/
      audio_0001.wav
      session.json
      events.jsonl
      audio_blocks.csv
      audio_timing.csv
      pretrigger.wav                 # only when requested history is available
      listening_10x.mp3               # optional
      listening_10x.mp3.json
    take02/ ...
```

Camera videos are segmented at 15 minutes by default, and also across camera
reconnection, shape changes, or frame-ID resets. Seek color frames are MP4 previews;
thermal pixel values remain in `seek_thermal.csv`. No individual PNG frames are
saved. The take report's `segments` map identifies files, stream names, frame counts,
timing CSVs, connection generations, and finalization status. An in-progress part
uses a `.partial.mp4` suffix until verification and finalization succeed.

FLIR preview videos use 30 fps; Seek preview fps follows the selected sampling
mode. Use CSVs for actual receipt timing rather than inferring timing from playback.
Logs map frames to their video part and index. The metadata schema remains version 2.

In **After shutter request** mode, one valid frame received after a successful
shutter SDK call is selected per request. Request/return/frame timestamps are
recorded; this does not prove when the physical shutter reopened. Custom shutter
mode must be applied first. SDK failures or selection timeouts raise warnings and
can trigger recovery; they do not by themselves stop the entire take.

**FLIR only** places segmented MP4s directly in the session folder and omits
timing/thermal files. **Microphone only** produces normal-speed MP3; failed conversion
retains recoverable temporary WAV. Changing between independent and combined
recording modes starts a separate session folder.

There is **no `microphone/` output for camera-only takes**. The main audio WAVs
are contiguous PCM segments starting at file sample zero for the shared take.
Their `session.json` records the corresponding `first_stream_sample`, estimated
start time, sample count, segment map, and status. Pre-trigger history is stored
separately and records both the requested duration and actual available samples.
Automatic MP3 export includes the main segments, excluding `pretrigger.wav`.

## Check a recording

1. Open `takeNN_sync.json`. Require `status: "complete"`, an empty `errors`
   array, a non-null T0, and an entry for Seek and every expected FLIR.
2. Seek's first offset should be zero. Inspect each FLIR's actual
   `offset_from_seek_ns`; do not assume it is zero. Check received, written, and
   dropped frame counts, `has_gaps`, `gaps`, `recovery_counts`, and every segment's
   status. A `complete_with_gaps` take needs review before analysis.
3. For an audio take, require `audio_status: "complete"`, positive `audio_samples`,
   and a microphone entry. Read its `timestamp_source` before interpreting its
   small estimated offset. For camera-only, expect `audio_status: "disabled"`.
4. Inspect camera CSV `relative_to_seek_ns` values and audio block/sample counts
   in `audio_timing.csv`. File sample zero must belong to the main WAV. Compare
   `shared_start_monotonic_ns` and `shared_stop_monotonic_ns` between the take
   report and microphone manifest; inspect fallback drift/latency limits above.
5. Listen to the MP3 and review the original WAV as appropriate. WAV/header
   format acceptance does not independently prove ultrasonic frequency response.
6. To verify physical timing, perform a connected-camera bench measurement
   using the common reference, including a representative-duration drift check.

Camera faults are reported per stream and trigger reconnection attempts while
other streams continue. Stale frames from previous connection generations are
rejected. Queue pressure records dropped frames and gaps. STOP cancels recovery
and finalizes retained data. Missing streams, audio capture/continuity failures,
and detected storage errors can mark the take incomplete or stop it.
Camera disk space is checked periodically; less than 1 GiB stops recording and
less than 10 GiB produces a warning.
The app finishes queued recording/export work during close. Recovery after a
power failure or an unreported driver loss is not guaranteed.

## Implementation map

`ultramic_studio/recorder/cameras/capture.py` acquires cameras and stamps frame receipt times.
`ultramic_studio/recorder/coordinator.py` owns the shared take state and reference clock.
`ultramic_studio/recorder/storage.py` writes camera outputs separately from callbacks.
`ultramic_studio/synchronized.py` adds timestamp-targeted sample selection to
the standalone recorder engine. `ultramic_studio/recorder/ui/window.py` reuses the standalone Qt
spectrogram, review, and export UI. `ultramic_studio/recorder/cameras/demo.py` supplies labelled synthetic
cameras for tests and demos without loading the camera SDKs.
