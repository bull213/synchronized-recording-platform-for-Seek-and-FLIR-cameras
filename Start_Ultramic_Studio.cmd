@echo off
setlocal
cd /d "%~dp0"
if exist "runtime\python.exe" (
    set "ULTRAMIC_PYTHON=runtime\python.exe"
    set "QT_PLUGIN_PATH="
    set "QT_QPA_PLATFORM_PLUGIN_PATH="
    set "PYQTGRAPH_QT_LIB=PySide6"
    goto run
)
if not exist ".venv\Scripts\python.exe" (
    goto anaconda
)
set "ULTRAMIC_PYTHON=.venv\Scripts\python.exe"
:run
"%ULTRAMIC_PYTHON%" -m ultramic_studio %*
if errorlevel 1 (
    echo.
    echo The app exited with an error. Copy the message above for troubleshooting.
    pause
)
exit /b

:anaconda
call "%~dp0Start_Anaconda.cmd" %*
exit /b
