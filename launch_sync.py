"""Compatibility launcher and PyInstaller entry point for the combined recorder."""
from ultramic_studio.recorder.cli import main


if __name__ == '__main__':
    raise SystemExit(main())
