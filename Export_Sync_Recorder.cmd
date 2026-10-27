@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo This export command runs on the development PC with its configured .venv.
    echo On another recording PC, use the exported ZIP. Python is included there.
    pause
    exit /b 1
)
echo Building the complete Windows app and transfer ZIP. This can take several minutes.
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements-export.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" scripts\build_sync_portable.py %*
if errorlevel 1 goto failed
echo.
echo Export complete. Open the exports folder and copy the newest ZIP to the new PC.
pause
exit /b 0
:failed
echo.
echo Export failed. Read the error above. No existing recordings or exports were replaced.
pause
exit /b 1
