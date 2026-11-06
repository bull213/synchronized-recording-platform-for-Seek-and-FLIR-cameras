# Cage ROI thermal exporter

In the combined recorder, choose **View → Cage ROI export…** to open the tool
in a separate window. A finished current session is offered automatically; use
**Open recording…** to choose another. You can also run **Setup_Windows.cmd --camera**
once, then double-click **Start_Cage_ROI.cmd** in the source project.

1. Open a **finished recording folder** containing `seek_thermal.csv` and
   `seek_log.csv`. The native thermal image appears immediately after the log is read.
2. Click **Add cage** and drag around one cage. Repeat for the other cage.
   Defaults are `WT_01`, `WT_02`, etc. Use **Rename / genotype** for other labels.
3. Click a rectangle to select it. Drag inside to move it, drag a corner to resize,
   or choose **Redraw selected**. Cage rectangles cannot overlap.
4. Use the time slider and **Preview selected time** to inspect cage positions
   later in the recording. This follows the log's video segment and frame index;
   playback seconds are not used as acquisition timestamps. Thermal export uses
   native pixels. **Native thermal frame** loads the calibrated thermal image at
   the slider position. The first jump forward scans frame offsets in the large
   source file; those offsets are cached for faster subsequent visits during the session.
   The cage thumbnail shows the entire ROI scaled down while preserving its shape;
   its caption reports the actual pixel dimensions. **Enlarge cage preview** opens
   a resizable view of the full crop. Display size does not change exported pixels.
5. Check **Cage boundaries checked for this recording**. Optionally save the
   selections for reuse. Loading selections requires reviewing them again;
   configurations with a different thermal resolution are rejected.
6. Choose whether to export cropped Seek video, click **Export pixels + temperatures**,
   then choose a destination parent folder. Video is selected by default in the recorder.
   Each export creates a new run folder and processes all frames and takes.

Finish the current take and disarm triggers before exporting. The recorder blocks
starting or arming a recording until export completes or is cancelled. The
standalone tool does not coordinate with a separate recorder process.

The banner above Export states whether the export has started. If it says
**Export not running**, follow its instruction (usually check the cage-boundary
checkbox). After choosing the output folder it changes to **EXPORT RUNNING**,
with an updating elapsed clock and processed-frame count. The progress bar shows
the percentage; the line below it estimates remaining time. **EXPORT COMPLETE**
means the files are saved and verified. Failure or cancellation remains visible
in the banner. Cancelling the folder dialog does not start an export.

The ROI is a fixed cage region, not a tracked mouse body. Check that the camera
and cages did not move. A full-cage mean includes bedding and other cage contents;
the maximum is a surface-temperature proxy, not a measurement of core temperature.

## Files and measurements

When video export is selected, each cage gets cropped Seek MP4 parts and a frame
map with source frame identifiers and timestamps. Source take and part boundaries
and playback rates are preserved; acquisition times come from the frame map.
Odd-sized crops are padded on the bottom or right for encoding; the thermal arrays
retain exact ROI dimensions. The videos are re-encoded visual references. Audio and
FLIR videos are not split.

Each mouse folder contains `pixels/part_000001.npz`, etc., and
`temperature_timeseries.csv`. At most 256 frames go into a pixel archive; the
chunk size is reduced for large selections to keep combined crop buffers within
64 MiB. Buffers are reused across chunks, and at most two cage archives compress
concurrently. The chunk finishes before its buffers are reused; work does not
accumulate in an unbounded queue. Arrays are losslessly compressed float64 values parsed from the original
stored decimal pixels, with no additional rounding or calibration.

NPZ arrays: `pixels` (frame, row, column), `source_frame_index`, `take_id`,
`timestamp_utc_ns`, `host_monotonic_ns`, and `calibration_profile_index`.
Load them using `numpy.load(path, allow_pickle=False)`; no pickle is required.
Each chunk is read back and every array (including IDs and calibration indices)
is checked before it is published. Standard NPZ/DEFLATE level 1 prioritizes export
speed over the smallest possible files. This does not change stored values or
require a different reader.

The summary CSV includes the same frame IDs and exact integer timestamps, a
nanosecond UTC ISO string, mouse/genotype labels, ROI maximum and mean,
warmest-5% mean, valid-pixel count, and quality flag. The warmest fraction includes
all ties at the 95th percentile. `temperature_c` equals `roi_max_c`, making this
CSV directly usable by the existing temperature figure loader. No temporal
resampling, interpolation, phase assignment, or baseline subtraction occurs here.

Nonfinite pixels are retained in the arrays. An affected frame has blank summary
temperatures and `quality=nonfinite_pixels`; it is not silently repaired or dropped.
Timestamp gaps remain gaps. The existing figure loader drops blank temperatures,
so use the exported quality column to audit coverage before plotting.

The run also includes `roi_config.json`, a labeled `roi_overview.png`, and
`manifest.json` with source paths, sizes/modification times, processed thermal
byte SHA-256, calibration profiles, settings, frame counts, and output inventory.
Coordinate convention is native `[x, y, width, height]`, equivalent to
`pixels[y:y+height, x:x+width]`, with the upper bounds excluded. The image widget
maps display positions back to these native coordinates despite resizing or margins.

Only a run with `status=complete` is finished. Cancelled or failed runs remain
explicitly marked, even if some output chunks exist. Cancellation stops at a safe
frame/chunk boundary. Resume is not implemented; start a new export. Source
recordings and earlier exports are never overwritten. A new run fails if source
files change during extraction. Export disk errors retain an incomplete manifest.

## Reproducible scripting

Use `thermal_roi.make_config(recording, shape, cages)` and
`thermal_roi.export_rois(recording, config, output_parent, include_video=True)`
to include video; the default remains thermal-only. A saved configuration
can be loaded with `thermal_roi.load_config`; a different source must be reviewed
and a new configuration made before extraction. The optional `max_frames` argument
is for validation samples and records `scope=sample` in the manifest. The GUI
always exports the complete recording.

This tool follows the native-coordinate selection and streaming-validation
patterns inspected in the local Mouse Sleep Analyzer. It does not import that
project or require sleep-analysis outputs.
