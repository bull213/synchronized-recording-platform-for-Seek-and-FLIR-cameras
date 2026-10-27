@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Create_Desktop_Shortcut.ps1"
if errorlevel 1 (
    echo Could not create the desktop shortcut. See the error above.
    pause
    exit /b 1
)
echo You can now start the app from the Seek FLIR Ultramic Recorder desktop shortcut.
pause
