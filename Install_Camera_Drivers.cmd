@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -Command "if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo Right-click this file and select Run as administrator.
    echo It installs the bundled Seek and FLIR USB3 camera drivers on this PC.
    pause
    exit /b 1
)
if not exist "drivers\Seek\seekusb.inf" goto missing
if not exist "drivers\FLIR_USB3\PGRUSBCam3.inf" goto missing
echo Installing the supplied Seek and FLIR USB3 drivers. Details: driver_install.log
pnputil.exe /add-driver "drivers\Seek\seekusb.inf" /install > driver_install.log 2>&1
set "SYNC_SEEK_DRIVER_RESULT=%errorlevel%"
pnputil.exe /add-driver "drivers\FLIR_USB3\PGRUSBCam3.inf" /install >> driver_install.log 2>&1
set "SYNC_FLIR_DRIVER_RESULT=%errorlevel%"
type driver_install.log
if not "%SYNC_SEEK_DRIVER_RESULT%"=="0" if not "%SYNC_SEEK_DRIVER_RESULT%"=="3010" goto failed
if not "%SYNC_FLIR_DRIVER_RESULT%"=="0" if not "%SYNC_FLIR_DRIVER_RESULT%"=="3010" goto failed
echo.
echo Driver packages added. Reconnect the cameras and run Setup_New_PC.cmd.
if "%SYNC_SEEK_DRIVER_RESULT%"=="3010" echo Restart Windows to finish the Seek driver installation.
if "%SYNC_FLIR_DRIVER_RESULT%"=="3010" echo Restart Windows to finish the FLIR driver installation.
pause
exit /b 0
:missing
echo Driver files are missing. Use the complete exported ZIP or the vendor SDK installers.
pause
exit /b 1
:failed
echo.
echo Windows did not complete a driver installation. See driver_install.log.
echo Use the vendor installer for your camera model; instructions are in START_SYNC_HERE.html.
pause
exit /b 1
