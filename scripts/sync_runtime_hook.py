"""Retain Windows DLL search directories for native camera and audio modules."""
import os
import sys
from pathlib import Path

root = Path(sys._MEIPASS)
sys._sync_recorder_dll_directories = []
for folder in (root, root / 'PySpin', root / 'camera_sdk' / 'seek', root / 'cv2'):
    if folder.is_dir() and hasattr(os, 'add_dll_directory'):
        sys._sync_recorder_dll_directories.append(os.add_dll_directory(str(folder)))
