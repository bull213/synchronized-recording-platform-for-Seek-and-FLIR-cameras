@echo off
setlocal
pushd "%~dp0" || exit /b 1
if exist "SyncRecorder.exe" (
    "SyncRecorder.exe" %*
    goto finished
)
if not exist ".venv\Scripts\python.exe" (
    echo Extract the complete exported SyncRecorder ZIP first.
    echo For editable source, use the configured Python environment described in README.md.
    pause
    popd
    exit /b 1
)
".venv\Scripts\python.exe" -m ultramic_studio.recorder %*

:finished
set "recorder_exit_code=%errorlevel%"
if not "%recorder_exit_code%"=="0" pause
popd
exit /b %recorder_exit_code%
