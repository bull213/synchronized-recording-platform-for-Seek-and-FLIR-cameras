"""Keep synthetic tests independent of personal settings and calibration data."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path):
    from PySide6 import QtCore

    settings = QtCore.QSettings
    settings.setDefaultFormat(settings.Format.IniFormat)
    for scope in (settings.Scope.UserScope, settings.Scope.SystemScope):
        settings.setPath(settings.Format.IniFormat, scope, str(tmp_path / 'preferences'))


@pytest.fixture
def example_calibration(monkeypatch):
    from seek_calibration import SEEK_CALIBRATIONS, SeekCalibration

    calibration = SeekCalibration('example-calibration', 'Synthetic test calibration', 1.2, 2.5)
    monkeypatch.setitem(SEEK_CALIBRATIONS, calibration.name, calibration)
    return calibration
