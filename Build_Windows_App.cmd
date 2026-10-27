@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run Setup_Windows.cmd first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install "pyinstaller==6.22.3"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onedir --windowed --name UltramicStudio --collect-all sounddevice --collect-all lameenc --collect-data pyqtgraph launch.py
if errorlevel 1 goto failed
echo.
echo Built dist\UltramicStudio\UltramicStudio.exe
echo Keep the entire dist\UltramicStudio folder together. Test on the recording PC.
pause
exit /b 0
:failed
echo Build failed. Read the error above. The normal Start script still works.
pause
exit /b 1
