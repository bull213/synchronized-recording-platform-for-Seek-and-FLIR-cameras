# Cage ROI data exporter (Seek thermal & FLIR optical)

In the camera recorder, choose **View → Cage ROI export…** to open the tool in a
separate window. A finished current session is offered automatically; use **Open
recording…** to choose another. The standalone **Start_Cage_ROI.cmd** launcher
also remains available in the source project.

1. Open a **finished recording folder** containing Seek thermal data (`seek_thermal.csv`),
   FLIR optical video (`flir_cam0*.mp4`), or both. The appropriate image appears immediately:
   - For Seek: the calibrated native thermal image is loaded. If MP4 video parts are missing,
     thermal-only mode is automatically engaged with video export safely disabled.
   - For FLIR: the optical camera frame is loaded, and controls adapt to video cropping.
   - For Combined: a stream selector allows switching between Seek thermal and FLIR camera views.
2. Click **Add cage** and drag around one cage. Repeat for the other cage.
   Defaults are `WT_01`, `WT_02`, etc. Use **Rename / genotype** for other labels.
3. Click a rectangle to select it. Drag inside to move it, drag a corner to resize,
   or choose **Redraw selected**. For keyboard entry, select a cage, enter native
   pixel **X**, **Y**, **Width**, and **Height**, then choose **Apply coordinates**.
   To create a cage without drawing, choose **Add cage** and apply its coordinates.
   Cage rectangles cannot overlap; valid edits require reviewing the boundaries again.
4. Use the time slider and **Preview selected time** to inspect cage positions
   later in the recording. If video parts are available, this follows the video frames.
   If video parts are missing in a Seek recording, the slider seamlessly decodes
   native thermal frames via the background cache. **Native thermal frame** loads
   the calibrated thermal image at the slider position (for Seek streams).
   The cage thumbnail shows the entire ROI scaled down while preserving its shape;
   its caption reports the actual pixel dimensions. **Enlarge cage preview** opens
   a resizable view of the full crop. Display size does not change exported pixels.
5. Check **Cage boundaries checked for this recording**. Optionally save the
   selections for reuse. Loading selections requires reviewing them again;
   configurations with a different resolution are rejected.
6. Choose whether to export cropped video, click **Export**, then choose a destination
   parent folder. Each export creates a new run folder and processes all frames and takes.

Finish the current take and disarm triggers before starting a cage export. While
the export runs, starting or arming a recording is blocked. Opening and reviewing
the tool remains available during capture. Closing the recorder cancels an active
export and waits for its worker to finish safely. The standalone launcher runs
independently and does not coordinate with another recorder process.

The banner above Export states whether the export has started. If it says
**Export not running**, follow its instruction (usually check the cage-boundary
checkbox). After choosing the output folder it changes to **EXPORT RUNNING**,
with an updating elapsed clock and processed-frame count. The progress bar shows
the percentage; the line below it estimates remaining time. **EXPORT COMPLETE**
means the files are saved and verified. Click **Plot thermal figure…** to immediately
launch the multi-mouse thermal plot dialog pre-populated with each cage's mouse ID,
genotype, and timeseries, or click **Open last export** to view the saved files.
Failure or cancellation remains visible in the banner. Cancelling the folder dialog
does not start an export.

The ROI is a fixed cage region, not a tracked mouse body. Check that the camera
and cages did not move. A full-cage mean includes bedding and other cage contents;
the maximum is a surface-temperature proxy, not a measurement of core temperature.

## Files and measurements

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
require a different reader. See [performance measurements](ROI_PERFORMANCE.md).

When video export is enabled, each cage also has `video_parts/` with cropped Seek
MP4s matching the source take and part boundaries, plus `video_frame_map.csv`
linking each output frame to the source frame index and exact acquisition times.
The videos retain the source playback rate; that rate is not an acquisition
timestamp. Odd-width or odd-height crops are padded by one pixel at the right or
bottom for the video encoder. Thermal arrays keep the exact selected dimensions.
FLIR videos and microphone audio are not split by this tool. Video is re-encoded
and is intended for visual review; use the thermal arrays for analysis.

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
to include cropped videos. The default is thermal-only for compatibility. A saved configuration
can be loaded with `thermal_roi.load_config`; a different source must be reviewed
and a new configuration made before extraction. The optional `max_frames` argument
is for validation samples and records `scope=sample` in the manifest. The GUI
always exports the complete recording.

This tool follows the native-coordinate selection and streaming-validation
patterns inspected in the local Mouse Sleep Analyzer. It does not import that
project or require sleep-analysis outputs.
