"""Recorder dialog for selecting CSV groups and thermal plot settings."""
from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6 import QtCore, QtWidgets as W

from ultramic_studio.recorder.group_thermal_plot import DEFAULT_PLOT_SETTINGS, parse_time_str


class GroupThermalPlotDialog(W.QDialog):
    """Edit and persist multi-mouse plot settings without editing Python files."""

    SETTINGS_KEY = 'group_plot/config'

    def __init__(self, parent, preferences, initial_dir):
        super().__init__(parent)
        self.preferences = preferences or QtCore.QSettings('UltramicStudio', 'GroupThermalPlot')
        self.result_data = None
        self._last_imported_roi_folder = None
        self._last_imported_cages = []
        self.setWindowTitle('Multi-mouse thermal plot settings')
        self.setMinimumWidth(760)

        saved = self.preferences.value(self.SETTINGS_KEY, '', type=str)
        try:
            data = json.loads(saved) if saved else {}
        except (TypeError, json.JSONDecodeError):
            data = {}

        self.groups = data.get('groups', {})
        self.input_dir = W.QLineEdit(data.get('input_dir', str(initial_dir)))
        self.output_dir = W.QLineEdit(data.get('output_dir', ''))
        self.output_name = W.QLineEdit(data.get('output_filename', DEFAULT_PLOT_SETTINGS['OUTPUT_FILENAME']))

        layout = W.QVBoxLayout(self)

        # File location header
        form = W.QFormLayout()
        layout.addLayout(form)
        form.addRow('Default data folder', self._directory_row(self.input_dir, 'Select default data folder'))

        # Data mode selector
        mode_box = W.QHBoxLayout()
        mode_label = W.QLabel('Data source:')
        mode_label.setStyleSheet('font-weight: bold;')
        self.mode_timeseries = W.QRadioButton('Pre-extracted timeseries (temperature_timeseries.csv — 1 file per mouse, instant)')
        self.mode_pixels = W.QRadioButton('Raw pixel chunks (pixels/part_*.npz — custom hotspot extraction)')
        self.mode_timeseries.setChecked(True)
        mode_box.addWidget(mode_label)
        mode_box.addWidget(self.mode_timeseries)
        mode_box.addWidget(self.mode_pixels)
        mode_box.addStretch(1)
        layout.addLayout(mode_box)

        # Group table
        table_label = W.QLabel('Groups and single-mouse thermal files (CSV or NPZ):')
        table_label.setStyleSheet('font-weight: bold; margin-top: 6px;')
        layout.addWidget(table_label)

        self.table = W.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(['Group / genotype', 'Thermal files / folders (1 mouse per entry; separate with ;)'])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 160)
        self.table.setMinimumHeight(150)
        layout.addWidget(self.table)

        group_buttons = W.QHBoxLayout()
        import_roi = W.QPushButton('Import Cage ROI export…')
        import_roi.setStyleSheet('font-weight: bold; background-color: #243447; color: #4c9aff; padding: 4px 10px;')
        add_group = W.QPushButton('Add group')
        remove_group = W.QPushButton('Remove selected group')
        add_files = W.QPushButton('Choose files for group…')
        add_folder = W.QPushButton('Choose mouse folder…')
        group_buttons.addWidget(import_roi)
        group_buttons.addWidget(add_group)
        group_buttons.addWidget(remove_group)
        group_buttons.addWidget(add_files)
        group_buttons.addWidget(add_folder)
        group_buttons.addStretch(1)
        layout.addLayout(group_buttons)

        import_roi.clicked.connect(lambda: self.import_cage_roi_folder())
        add_group.clicked.connect(lambda: self._add_group())
        remove_group.clicked.connect(self._remove_group)
        add_files.clicked.connect(self._choose_files)
        add_folder.clicked.connect(self._choose_folder)
        self.mode_timeseries.toggled.connect(self._update_table_from_cages)

        init_path = Path(initial_dir) if initial_dir else None
        if init_path and ((init_path / 'roi_config.json').is_file() or (init_path / 'manifest.json').is_file()):
            self.import_cage_roi_folder(init_path)
        else:
            for name, files in self.groups.items():
                self._add_group(name, files)
            if self.table.rowCount() == 0:
                self._add_group('WT')
                self._add_group('Dravet')

        # Analysis options
        options_label = W.QLabel('Analysis and lighting parameters:')
        options_label.setStyleSheet('font-weight: bold; margin-top: 10px;')
        layout.addWidget(options_label)

        options = W.QFormLayout()
        layout.addLayout(options)
        self.controls = {}

        # Start Time (Time 0 clock alignment)
        saved_start = data.get('START_TIME', data.get('start_time', '07:00'))
        st = parse_time_str(saved_start)
        self.start_time = W.QTimeEdit(QtCore.QTime(st.hour, st.minute))
        self.start_time.setDisplayFormat('HH:mm')
        self.start_time.setToolTip('Reference clock time (e.g. 05:00 or 07:00). All mice align with Time 0 at this clock time.')
        options.addRow('Start time (Time 0 clock)', self.start_time)
        self.controls['start_time'] = self.start_time

        # Time Window (e.g. 24h, 48h, etc.)
        saved_win = int(data.get('TIME_WINDOW_HOURS', data.get('plot_hours', 24)))
        self.time_window = W.QSpinBox()
        self.time_window.setRange(1, 336)
        self.time_window.setValue(saved_win)
        self.time_window.setSuffix(' hours')
        self.time_window.setToolTip('Duration of analysis window (e.g. 24h, 48h, etc.).')
        options.addRow('Time window', self.time_window)
        self.controls['time_window'] = self.time_window
        self.controls['plot_hours'] = self.time_window

        # Light-Dark Cycle
        cycle_box = W.QHBoxLayout()
        saved_on = data.get('LIGHTS_ON_TIME', f"{data.get('lights_on', 7):02d}:00")
        on_t = parse_time_str(saved_on)
        self.lights_on = W.QTimeEdit(QtCore.QTime(on_t.hour, on_t.minute))
        self.lights_on.setDisplayFormat('HH:mm')
        self.lights_on.setToolTip('Clock time when lights turn ON in the animal facility.')

        saved_off = data.get('LIGHTS_OFF_TIME', f"{data.get('lights_off', 19):02d}:00")
        off_t = parse_time_str(saved_off)
        self.lights_off = W.QTimeEdit(QtCore.QTime(off_t.hour, off_t.minute))
        self.lights_off.setDisplayFormat('HH:mm')
        self.lights_off.setToolTip('Clock time when lights turn OFF in the animal facility.')

        cycle_box.addWidget(W.QLabel('Lights ON:'))
        cycle_box.addWidget(self.lights_on)
        cycle_box.addSpacing(16)
        cycle_box.addWidget(W.QLabel('Lights OFF:'))
        cycle_box.addWidget(self.lights_off)
        cycle_box.addStretch(1)
        options.addRow('Light–dark cycle', cycle_box)
        self.controls['lights_on'] = self.lights_on
        self.controls['lights_off'] = self.lights_off

        # Facility Timezone
        self._add_line(options, 'timezone', 'Facility timezone (IANA)', data.get('timezone', DEFAULT_PLOT_SETTINGS['TIMEZONE']))

        # Elapsed time bin
        self._add_spin(options, 'bin_minutes', 'Elapsed-time bin (minutes)', 1, 1440, 30, data)

        # Sampling match toggle
        self.match_common = W.QCheckBox('Match 1-second sampling across all mice (equal observation opportunity)')
        self.match_common.setChecked(bool(data.get('MATCH_COMMON_SUPPORT', True)))
        options.addRow('', self.match_common)
        self.controls['match_common'] = self.match_common

        # Output options (PNG only)
        out_label = W.QLabel('Output figure settings (PNG):')
        out_label.setStyleSheet('font-weight: bold; margin-top: 10px;')
        layout.addWidget(out_label)

        out_form = W.QFormLayout()
        layout.addLayout(out_form)
        out_form.addRow('PNG output folder (blank = default)', self._directory_row(self.output_dir, 'Select PNG output folder', allow_blank=True))
        out_form.addRow('PNG filename', self.output_name)

        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok | W.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _directory_row(self, edit, title, allow_blank=False):
        row = W.QWidget()
        box = W.QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(edit, 1)
        button = W.QPushButton('Browse…')
        box.addWidget(button)

        def browse():
            start = edit.text() or str(Path(self.input_dir.text() or Path.cwd()))
            selected = W.QFileDialog.getExistingDirectory(self, title, start)
            if selected:
                edit.setText(selected)
        button.clicked.connect(browse)
        return row

    def _add_group(self, name='', files=None):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, W.QTableWidgetItem(str(name)))
        self.table.setItem(row, 1, W.QTableWidgetItem('; '.join(files or [])))

    def _remove_group(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _choose_files(self):
        row = self.table.currentRow()
        if row < 0:
            W.QMessageBox.information(self, 'Choose files', 'Select a group row first.')
            return
        start_folder = self.input_dir.text() or str(Path.cwd())
        paths, _ = W.QFileDialog.getOpenFileNames(
            self, 'Select one or more single-mouse thermal files', start_folder,
            'Thermal files (*.csv *.npz);;NumPy archives (*.npz);;CSV files (*.csv);;All files (*.*)')
        if not paths:
            return

        # Store filenames or paths relative to input_dir if possible
        default_folder = Path(self.input_dir.text() or Path.cwd()).resolve()
        stored = []
        for p in paths:
            p_res = Path(p).resolve()
            try:
                rel = p_res.relative_to(default_folder)
                stored.append(str(rel))
            except ValueError:
                stored.append(str(p_res))

        self.table.item(row, 1).setText('; '.join(stored))

    def _choose_folder(self):
        row = self.table.currentRow()
        if row < 0:
            W.QMessageBox.information(self, 'Choose folder', 'Select a group row first.')
            return
        start_folder = self.input_dir.text() or str(Path.cwd())
        folder = W.QFileDialog.getExistingDirectory(self, 'Select mouse run or pixels folder', start_folder)
        if not folder:
            return
        default_folder = Path(self.input_dir.text() or Path.cwd()).resolve()
        p_res = Path(folder).resolve()
        try:
            rel = str(p_res.relative_to(default_folder))
        except ValueError:
            rel = str(p_res)
        current = [f.strip() for f in self.table.item(row, 1).text().split(';') if f.strip()]
        if rel not in current:
            current.append(rel)
        self.table.item(row, 1).setText('; '.join(current))

    def import_cage_roi_folder(self, folder: Path | str | None = None):
        """Import cage configuration from a CAGE ROI export run directory."""
        if not folder:
            start = self.input_dir.text() or str(Path.cwd())
            folder = W.QFileDialog.getExistingDirectory(self, 'Select Cage ROI export folder', start)
            if not folder:
                return
        run_path = Path(folder).resolve()
        config_path = run_path / 'roi_config.json'
        manifest_path = run_path / 'manifest.json'

        cages = []
        if config_path.is_file():
            try:
                data = json.loads(config_path.read_text(encoding='utf-8'))
                cages = data.get('cages', [])
            except Exception:
                pass
        if not cages and manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text(encoding='utf-8'))
                cages = data.get('config', {}).get('cages', [])
            except Exception:
                pass

        if not cages:
            # Fallback: scan for mouse subdirectories with timeseries or pixels
            subdirs = [p for p in run_path.iterdir()
                       if p.is_dir() and ((p / 'temperature_timeseries.csv').is_file() or (p / 'pixels').is_dir())]
            for sd in sorted(subdirs):
                genotype = 'WT' if 'wt' in sd.name.lower() else 'Dravet'
                cages.append({'mouse_id': sd.name, 'genotype': genotype})

        if not cages:
            W.QMessageBox.warning(self, 'Invalid ROI run',
                                  f'No cage selections found in {run_path.name}.\n'
                                  'Select a run folder containing roi_config.json or mouse folders.')
            return

        self._last_imported_roi_folder = run_path
        self._last_imported_cages = cages
        self.input_dir.setText(str(run_path))
        self._update_table_from_cages()

    def _update_table_from_cages(self):
        """Re-populate group table using the imported cages and current data mode."""
        if not getattr(self, '_last_imported_cages', None):
            return
        run_path = self._last_imported_roi_folder
        use_pixels = hasattr(self, 'mode_pixels') and self.mode_pixels.isChecked()

        groups = {}
        for c in self._last_imported_cages:
            m_id = c['mouse_id']
            genotype = c.get('genotype', 'WT').strip() or 'WT'
            if use_pixels and (run_path / m_id / 'pixels').is_dir():
                target = f"{m_id}/pixels"
            elif (run_path / m_id / 'temperature_timeseries.csv').is_file():
                target = f"{m_id}/temperature_timeseries.csv"
            else:
                target = m_id
            groups.setdefault(genotype, []).append(target)

        self.table.setRowCount(0)
        for name, files in groups.items():
            self._add_group(name, files)

    def _add_spin(self, form, key, label, minimum, maximum, default, saved):
        control = W.QSpinBox()
        control.setRange(minimum, maximum)
        control.setValue(int(saved.get(key, default)))
        form.addRow(label, control)
        self.controls[key] = control

    def _add_line(self, form, key, label, value):
        control = W.QLineEdit(str(value))
        form.addRow(label, control)
        self.controls[key] = control

    def _accept(self):
        groups = {}
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 0).text().strip()
            filenames = [f.strip() for f in self.table.item(row, 1).text().split(';') if f.strip()]
            if not name or not filenames:
                continue
            if name in groups:
                W.QMessageBox.warning(self, 'Invalid groups', f'Duplicate group name: {name}')
                return
            groups[name] = filenames
        if not groups:
            W.QMessageBox.warning(self, 'Invalid groups', 'Add at least one group with single-mouse thermal files (CSV or NPZ).')
            return

        tz_str = self.controls['timezone'].text().strip()
        try:
            ZoneInfo(tz_str)
        except (ZoneInfoNotFoundError, ValueError):
            W.QMessageBox.warning(self, 'Invalid timezone', 'Enter a valid IANA timezone, such as Asia/Jerusalem.')
            return

        filename = self.output_name.text().strip()
        if not filename:
            W.QMessageBox.warning(self, 'Invalid filename', 'Enter a PNG filename.')
            return
        if not filename.lower().endswith('.png'):
            filename += '.png'

        start_time_str = self.start_time.time().toString('HH:mm')
        on_time_str = self.lights_on.time().toString('HH:mm')
        off_time_str = self.lights_off.time().toString('HH:mm')
        win_hours = self.time_window.value()

        settings = {
            'START_TIME': start_time_str,
            'TIME_WINDOW_HOURS': win_hours,
            'PLOT_HOURS': win_hours,
            'LIGHTS_ON_TIME': on_time_str,
            'LIGHTS_OFF_TIME': off_time_str,
            'LIGHTS_ON_HOUR': self.lights_on.time().hour(),
            'LIGHTS_OFF_HOUR': self.lights_off.time().hour(),
            'TIMEZONE': tz_str,
            'BIN_MINUTES': self.controls['bin_minutes'].value(),
            'MATCH_COMMON_SUPPORT': self.match_common.isChecked(),
            'OUTPUT_DIR': self.output_dir.text().strip() or None,
            'OUTPUT_FILENAME': filename,
            'DPI': DEFAULT_PLOT_SETTINGS['DPI'],
            'VALID_TEMPERATURE_C': DEFAULT_PLOT_SETTINGS['VALID_TEMPERATURE_C'],
            'LEGACY_CALIBRATION': DEFAULT_PLOT_SETTINGS['LEGACY_CALIBRATION'],
            # Retain legacy keys for older test harnesses
            'max_start_minutes': 60,
            'roi_size': 7,
            'plot_hours': win_hours,
            'lights_on': self.lights_on.time().hour(),
            'lights_off': self.lights_off.time().hour(),
            'timezone': tz_str,
            'output_filename': filename,
        }

        self.result_data = {
            'input_dir': self.input_dir.text().strip(),
            'groups': groups,
            **settings,
        }
        self.preferences.setValue(self.SETTINGS_KEY, json.dumps(self.result_data))
        self.accept()
