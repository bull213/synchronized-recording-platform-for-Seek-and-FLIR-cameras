@echo off
setlocal
cd /d "%~dp0"
if not exist "SyncRecorder.exe" (
    echo Extract the complete exported ZIP, then run this file inside its SyncRecorder folder.
    pause
    exit /b 1
)
echo Checking software libraries. These checks do not record from your devices.
"SyncRecorder.exe" --check-setup --report setup_report.txt
if errorlevel 1 goto failed
echo.
echo Testing recording and export with synthetic cameras and audio.
"SyncRecorder.exe" --self-test --report self_test_report.txt
if errorlevel 1 goto failed
echo.
echo Software setup passed. No Python installation or package downloads are needed.
echo Connect the cameras and double-click Start_Sync_Recorder.cmd.
echo If the cameras are not detected, run Install_Camera_Drivers.cmd as administrator once.
echo The microphone is optional; Cameras only is the default.
pause
exit /b 0
:failed
echo.
echo A check failed. See the messages above and setup_report.txt / self_test_report.txt.
echo Keep SyncRecorder.exe and its entire _internal folder together.
pause
exit /b 1
