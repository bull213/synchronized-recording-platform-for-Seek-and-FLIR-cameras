@echo off
setlocal
cd /d "%~dp0"
if exist "runtime\python.exe" (
    set "ULTRAMIC_PYTHON=runtime\python.exe"
    goto run
)
if not exist ".venv\Scripts\python.exe" (
    goto anaconda
)
set "ULTRAMIC_PYTHON=.venv\Scripts\python.exe"
:run
"%ULTRAMIC_PYTHON%" -m ultramic_studio.diagnostics --probe
echo.
echo Results saved to microphone_diagnostics.txt in this folder.
pause
exit /b

:anaconda
call "%~dp0Start_Anaconda.cmd" --check
exit /b
