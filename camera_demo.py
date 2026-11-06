"""Compatibility alias; implementation lives in the recorder package."""
import importlib
import sys

_module = importlib.import_module('ultramic_studio.recorder.cameras.demo')
sys.modules[__name__] = _module
