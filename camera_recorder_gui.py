"""Compatibility alias; implementation lives in the recorder package."""
import importlib
import sys

_module = importlib.import_module('ultramic_studio.recorder.ui.window')
sys.modules[__name__] = _module
