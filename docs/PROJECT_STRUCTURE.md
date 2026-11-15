# Project structure

The repository keeps the Windows launchers at the root for easy access. The
recorder implementation is organized as a Python package, with supporting
documentation, examples, firmware, and tests alongside it.

| Path | Purpose |
| --- | --- |
| `ultramic_studio/` | Standalone microphone app and combined recorder packages. |
| `ultramic_studio/recorder/cameras/` | Seek and FLIR acquisition, SDK integration, and previews. |
| `ultramic_studio/recorder/roi/` | Cage ROI selection, preview, storage, and export. |
| `ultramic_studio/recorder/ui/` | Combined recorder window and presentation helpers. |
| `docs/` | Setup, synchronization, interface, and validation guides. |
| `docs/images/` | Screenshots and illustrative thermal output used by documentation. |
| `examples/` | Small trigger clients and usage examples. |
| `firmware/` | Optional Arduino TTL trigger bridge. |
| `scripts/` | Build, source packaging, and synthetic screenshot tools. |
| `tests/` | Automated tests using synthetic media and mocked devices. |
| `.github/workflows/` | Windows CI checks for tests, demo recording, and source inventory. |

Root-level Python modules such as `camera_recorder_gui.py` and `thermal_roi.py`
are compatibility entry points. New implementation code belongs in
`ultramic_studio/`; keep the root aliases and Windows launch scripts available
for existing commands and user workflows.

The root `README.md` is the GitHub landing page. `SOURCE_FILES.txt` is the
explicit public source inventory used by the package checker and portable
source exports. Add each new public documentation or example asset to that
inventory; build outputs, SDKs, recordings, and local configuration stay out
of the repository.
