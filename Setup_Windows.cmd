@echo off
setlocal
cd /d "%~dp0"
set "ULTRAMIC_REQUIREMENTS=requirements.txt"
if /i "%~1"=="--camera" set "ULTRAMIC_REQUIREMENTS=requirements-camera.txt"
if /i "%~1"=="--dev" set "ULTRAMIC_REQUIREMENTS=requirements-dev.txt"
if not "%~1"=="" if /i not "%~1"=="--camera" if /i not "%~1"=="--dev" (
    echo Usage: Setup_Windows.cmd [--camera ^| --dev]
    exit /b 1
)
echo Ultramic Studio - first-time setup
echo.
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
    %ULTRAMIC_PYTHON% -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r "%ULTRAMIC_REQUIREMENTS%"
if errorlevel 1 goto failed
echo.
echo Setup complete. Double-click Start_Ultramic_Studio.cmd to open the app.
if /i not "%ULTRAMIC_REQUIREMENTS%"=="requirements.txt" echo For the combined synthetic demo, run Demo_Sync_Recorder.cmd.
pause
exit /b 0

:failed
echo.
echo Setup did not complete. Check the error above and your Internet connection.
echo Use 64-bit Python 3.11, 3.12, or 3.13. Python 3.12 is recommended.
pause
exit /b 1
