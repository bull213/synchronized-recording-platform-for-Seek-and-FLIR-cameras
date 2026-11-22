@echo off
setlocal
cd /d "%~dp0"
if not exist "SyncRecorder.exe" (
    echo Extract the complete exported ZIP, then run this file inside its SyncRecorder folder.
    pause
    exit /b 1
)
echo ===============================================================================
echo Seek / FLIR / Ultramic Recorder - Setup Check
echo ===============================================================================
echo.
echo [1/2] Checking software libraries and runtime dependencies...
"SyncRecorder.exe" --check-setup --report setup_report.txt
if errorlevel 1 goto failed
echo.
echo [2/2] Running synthetic recorder self-test (cameras + audio)...
"SyncRecorder.exe" --self-test --report self_test_report.txt
if errorlevel 1 goto failed

echo.
echo ===============================================================================
echo Software Setup Check Passed!
echo ===============================================================================
echo No Python installation or external package downloads are needed.
echo.
echo CAMERA DRIVERS:
echo   - If your cameras are connected and detected, launch Start_Sync_Recorder.cmd.
echo   - If cameras are NOT detected:
echo       1. Right-click Install_Camera_Drivers.cmd and select "Run as administrator".
echo       2. If Windows Device Manager still shows a warning on Seek Thermal Camera,
echo          right-click it in Device Manager, choose "Update driver", and browse to:
echo          "%~dp0drivers\Seek"
echo.
echo For detailed instructions, refer to docs\CAMERA_DRIVERS.md and START_SYNC_HERE.html.
echo ===============================================================================
echo.
pause
exit /b 0

:failed
echo.
echo ===============================================================================
echo [ERROR] Setup check failed.
echo ===============================================================================
echo Review the error messages above and check setup_report.txt / self_test_report.txt.
echo Ensure SyncRecorder.exe and its complete _internal folder are in the same folder.
echo.
pause
exit /b 1
