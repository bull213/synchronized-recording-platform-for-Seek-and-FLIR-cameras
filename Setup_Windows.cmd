@echo off
setlocal
cd /d "%~dp0"
echo ===============================================================================
echo Seek / FLIR / Ultramic Studio - Environment Setup
echo ===============================================================================
echo.

set "REQ_FILE=requirements-camera.txt"
if /i "%~1"=="--dev" set "REQ_FILE=requirements-dev.txt"
if /i "%~1"=="--audio-only" set "REQ_FILE=requirements.txt"
if /i "%~1"=="--camera" set "REQ_FILE=requirements-camera.txt"
if not exist "%REQ_FILE%" set "REQ_FILE=requirements.txt"

py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "ULTRAMIC_PYTHON=py -3.12"
    goto found
)
py -3.13 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "ULTRAMIC_PYTHON=py -3.13"
    goto found
)
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "ULTRAMIC_PYTHON=py -3.11"
    goto found
)
python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "ULTRAMIC_PYTHON=python"
    goto found
)
echo Python was not found. Install 64-bit Python 3.12 from https://www.python.org/downloads/windows/
echo Include the Python launcher during installation, then run this file again.
pause
exit /b 1

:found
%ULTRAMIC_PYTHON% -c "import sys,struct; assert sys.version_info[:2] in [(3,11),(3,12),(3,13)] and struct.calcsize('P') == 8, 'Use 64-bit Python 3.11, 3.12, or 3.13'"
if errorlevel 1 goto failed
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv...
    %ULTRAMIC_PYTHON% -m venv .venv
    if errorlevel 1 goto failed
)
echo Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed

echo Installing dependencies from %REQ_FILE%...
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r "%REQ_FILE%"
if errorlevel 1 goto failed

echo.
echo ===============================================================================
echo Environment Setup Complete!
echo.
echo For camera recording:
echo   1. If not already done, right-click Install_Camera_Drivers.cmd and choose
echo      "Run as administrator" to install Seek and FLIR USB drivers.
echo   2. Double-click Start_Sync_Recorder.cmd to open the combined camera recorder.
echo.
echo For standalone microphone only:
echo   - Double-click Start_Ultramic_Studio.cmd
echo ===============================================================================
echo.
pause
exit /b 0

:failed
echo.
echo Setup did not complete. Check the error above and your Internet connection.
echo Use 64-bit Python 3.11, 3.12, or 3.13. Python 3.12 is recommended.
pause
exit /b 1
