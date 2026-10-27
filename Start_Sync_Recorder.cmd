@echo off
setlocal
cd /d "%~dp0"
if exist "SyncRecorder.exe" (
    "SyncRecorder.exe" %*
    if errorlevel 1 pause
    exit /b
)
if not exist ".venv\Scripts\python.exe" (
    echo Run Setup_Windows.cmd --camera first to install the source dependencies.
    echo See docs\SETUP.md for camera SDK installation or use the synthetic demo.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" launch_sync.py %*
if errorlevel 1 pause
