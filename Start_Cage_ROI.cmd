@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The project Python environment is missing. Run Setup_Windows.cmd first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" thermal_roi_gui.py %*
if errorlevel 1 pause
