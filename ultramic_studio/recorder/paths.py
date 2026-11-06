"""Application paths shared by source launches and frozen Windows exports."""
from pathlib import Path
import sys


def application_folder() -> Path:
    """Return the executable directory for a bundle, or the project root."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_folder() -> Path:
    """Return bundled resources, falling back to the source project root."""
    return Path(getattr(sys, '_MEIPASS', application_folder()))
