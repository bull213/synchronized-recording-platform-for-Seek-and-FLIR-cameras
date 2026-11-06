"""Room-specific conversions from Seek camera Celsius to calibrated Celsius."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeekCalibration:
    name: str
    label: str
    scale: float = 1.0
    offset_c: float = 0.0

    def apply(self, camera_c):
        """Convert a temperature or pixel array without modifying the input."""
        if self.name == 'none':
            return camera_c
        return self.scale * camera_c + self.offset_c

    def metadata(self):
        return dict(name=self.name, scale=self.scale, offset_c=self.offset_c,
                    input_units='C', output_units='C')


SEEK_CALIBRATIONS = {
    'none': SeekCalibration('none', 'None (camera temperature)'),
}


def get_seek_calibration(name):
    try:
        return SEEK_CALIBRATIONS[name]
    except KeyError:
        raise ValueError(f'Unknown Seek calibration: {name!r}.') from None
