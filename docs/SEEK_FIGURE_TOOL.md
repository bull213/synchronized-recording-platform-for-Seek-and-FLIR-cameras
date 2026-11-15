# Seek multi-mouse temperature figure tool

This portable script creates the three-panel 24-hour temperature summary from
one native Seek `seek_thermal.csv` per mouse. Each file contains the full camera
pixel matrix for every frame. The script accepts any number of mice and group
labels.

## Inputs

Pass one CSV for each mouse. If the files share a name such as
`seek_thermal.csv`, keep them in separate folders. The script uses each
parent-folder name as the mouse ID by default. Provide group labels in the same
order as the CSV paths with `--groups`:

```text
recordings/
  Mouse_A/seek_thermal.csv
  Mouse_B/seek_thermal.csv
  Mouse_C/seek_thermal.csv
```

The tool reads Seek frame headers and full-frame pixel rows directly; it does
not require Cage ROI export sidecars. For every selected frame, it finds the
hottest pixel in the camera frame and averages the finite values in the centered
7×7 neighborhood. At camera-frame edges, the neighborhood is clipped to
available pixels.

## Install and run

Install 64-bit Python 3.12. Extract `Seek_Group_Figure_Tool.zip`, open Command
Prompt in the extracted folder, and install dependencies:

```bat
py -3.12 -m pip install -r requirements.txt
```

Pass one group label per CSV, in the same order. Quote paths containing spaces:

```bat
py -3.12 create_seek_group_figure.py "D:\Data\Mouse_A\seek_thermal.csv" "D:\Data\Mouse_B\seek_thermal.csv" "D:\Data\Mouse_C\seek_thermal.csv" --groups WT WT Dravet --output "D:\Data\temperature_summary_24h.png"
```

By default, the output is `temperature_summary_24h.png` in the current folder.
The script also writes PDF, SVG, and a `_profiles.csv` beside it. The default
timezone is `Asia/Jerusalem`, with lights on at 10:30 and off at 22:30. The
first frame in the first input CSV defines the 24-hour window by default. Add
`--mouse-ids Mouse_A Mouse_B Mouse_C` to set display IDs explicitly.

Choose a local start explicitly when needed:

```bat
py -3.12 create_seek_group_figure.py "D:\Data\Mouse_A\seek_thermal.csv" "D:\Data\Mouse_B\seek_thermal.csv" --groups WT Dravet --window-start 2026-11-01T10:30 --timezone Asia/Jerusalem --lights-on 10:30 --lights-off 22:30
```

Each file must cover the same full 24-hour window. The plots show light/dark
phase means, per-mouse and group 24-hour profiles, and each group's
dark-minus-light values. Group colors and sample counts follow the supplied
labels. Shaded profile bands show between-mouse SD for groups with more than
one mouse.

The figure is a descriptive surface-temperature summary, not a core-temperature
measurement or a statistical genotype test. Choose the light schedule before
interpreting results.
