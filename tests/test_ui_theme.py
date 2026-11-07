"""Verify contrast of the palette used by recording controls and focus."""
import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtWidgets
from ultramic_studio.theme import CHECKMARK, COLORS, RECORDER_STYLE


def contrast(foreground, background):
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
                  for value in channels]
        return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

    a, b = luminance(foreground), luminance(background)
    return (max(a, b) + .05) / (min(a, b) + .05)


def test_readable_text_and_record_button_contrast():
    for background in ('bg', 'surface', 'input'):
        assert contrast(COLORS['text'], COLORS[background]) >= 4.5
    assert contrast(COLORS['muted'], COLORS['surface']) >= 4.5
    for background in ('record', 'record_hover', 'record_press'):
        assert contrast('#ffffff', COLORS[background]) >= 4.5
    assert contrast('#70b3ff', COLORS['input']) >= 3


def test_checked_checkbox_has_a_visible_tick():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    assert Path(CHECKMARK).is_file()
    checkbox = QtWidgets.QCheckBox('Test')
    checkbox.setStyleSheet(RECORDER_STYLE)
    checkbox.setChecked(True)
    checkbox.show()
    app.processEvents()
    option = QtWidgets.QStyleOptionButton()
    option.initFrom(checkbox)
    rect = checkbox.style().subElementRect(QtWidgets.QStyle.SubElement.SE_CheckBoxIndicator, option, checkbox)
    pixmap = checkbox.grab()
    image = pixmap.toImage()
    scale = pixmap.devicePixelRatio()
    pixels = [image.pixelColor(x, y) for y in range(int(rect.top() * scale), int((rect.bottom() + 1) * scale))
              for x in range(int(rect.left() * scale), int((rect.right() + 1) * scale))]
    assert any(color.blue() > 150 and color.red() < 150 for color in pixels)
    assert any(color.red() < 60 and color.green() < 75 and color.blue() < 100 for color in pixels)
    checkbox.close()
