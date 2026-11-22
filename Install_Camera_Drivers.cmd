@echo off
setlocal
cd /d "%~dp0"

echo ===============================================================================
echo Seek / FLIR Camera Driver Installer
echo ===============================================================================
echo.

powershell.exe -NoProfile -Command "if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo [ERROR] Administrator privileges required.
    echo.
    echo Please right-click Install_Camera_Drivers.cmd and choose "Run as administrator".
    echo This is required so Windows PnP can register the Seek and FLIR USB3 drivers.
    echo.
    pause
    exit /b 1
)

if not exist "drivers\Seek\seekusb.inf" goto missing
if not exist "drivers\FLIR_USB3\PGRUSBCam3.inf" goto missing

echo Installing Seek WinUSB driver (seekusb.inf)...
pnputil.exe /add-driver "drivers\Seek\seekusb.inf" /install > driver_install.log 2>&1
set "SYNC_SEEK_DRIVER_RESULT=%errorlevel%"

echo Installing FLIR USB3 driver (PGRUSBCam3.inf)...
pnputil.exe /add-driver "drivers\FLIR_USB3\PGRUSBCam3.inf" /install >> driver_install.log 2>&1
set "SYNC_FLIR_DRIVER_RESULT=%errorlevel%"

echo.
echo ----------------------- PnPUtil Output Log -----------------------
type driver_install.log
echo ------------------------------------------------------------------
echo.

if not "%SYNC_SEEK_DRIVER_RESULT%"=="0" if not "%SYNC_SEEK_DRIVER_RESULT%"=="3010" goto failed
if not "%SYNC_FLIR_DRIVER_RESULT%"=="0" if not "%SYNC_FLIR_DRIVER_RESULT%"=="3010" goto failed

echo [SUCCESS] Driver packages registered successfully!
if "%SYNC_SEEK_DRIVER_RESULT%"=="3010" echo [NOTE] Restart Windows to complete the Seek driver installation.
if "%SYNC_FLIR_DRIVER_RESULT%"=="3010" echo [NOTE] Restart Windows to complete the FLIR driver installation.
echo.
echo ===============================================================================
echo IMPORTANT: WINDOWS DEVICE MANAGER STEP
echo ===============================================================================
echo If your Seek Thermal Camera is already plugged in and not detected by the app,
echo or shows a yellow warning icon in Device Manager:
echo.
echo   1. Press Win + X, then click "Device Manager".
echo   2. Locate "Seek Thermal Camera" (under "Other devices" or "Cameras").
echo   3. Right-click it and choose "Update driver".
echo   4. Select "Browse my computer for drivers".
echo   5. Browse to the drivers\Seek folder in this directory:
echo      "%~dp0drivers\Seek"
echo   6. Click "Next" to complete the installation.
echo.
echo Next steps: Reconnect your camera USB cables, then run Setup_New_PC.cmd or
echo Start_Sync_Recorder.cmd.
echo ===============================================================================
echo.
pause
exit /b 0

:missing
echo [ERROR] Required driver files were not found:
if not exist "drivers\Seek\seekusb.inf" echo   - Missing: drivers\Seek\seekusb.inf
if not exist "drivers\FLIR_USB3\PGRUSBCam3.inf" echo   - Missing: drivers\FLIR_USB3\PGRUSBCam3.inf
echo.
echo Please ensure you extracted the complete ZIP including the 'drivers' folder.
echo For more help, see docs\CAMERA_DRIVERS.md.
echo.
pause
exit /b 1

:failed
echo.
echo [WARNING] Windows PnPUtil returned an unexpected status code. See driver_install.log.
echo.
echo If automatic installation did not bind the camera, update it via Device Manager:
echo   1. Press Win + X and select "Device Manager".
echo   2. Right-click the camera device and choose "Update driver".
echo   3. Select "Browse my computer for drivers" and choose:
echo      "%~dp0drivers\Seek"
echo   4. Click "Next" to install seekusb.inf directly.
echo.
echo See docs\CAMERA_DRIVERS.md or START_SYNC_HERE.html for vendor installer links.
echo.
pause
exit /b 1
