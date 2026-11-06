"""Compatibility alias; implementation lives in the recorder package."""
import importlib
import sys

_module = importlib.import_module('ultramic_studio.recorder.roi.gui')
if __name__ == '__main__':
    raise SystemExit(_module.main())
sys.modules[__name__] = _module
