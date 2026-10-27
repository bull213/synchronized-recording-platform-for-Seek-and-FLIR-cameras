@echo off
setlocal
cd /d "%~dp0"
if defined CONDA_EXE if exist "%CONDA_EXE%" (
    set "ULTRAMIC_CONDA=%CONDA_EXE%"
    goto found
)
where conda >nul 2>&1
if not errorlevel 1 (
    set "ULTRAMIC_CONDA=conda"
    goto found
)
for %%D in ("%USERPROFILE%\anaconda3" "%LOCALAPPDATA%\anaconda3" "%ProgramData%\anaconda3" "%USERPROFILE%\miniconda3" "%LOCALAPPDATA%\miniconda3" "%ProgramData%\miniconda3") do (
    if exist "%%~D\condabin\conda.bat" (
        set "ULTRAMIC_CONDA=%%~D\condabin\conda.bat"
        goto found
    )
)
echo Anaconda was not found in the usual locations.
echo Open Anaconda Prompt, type conda activate ultramic, then run this file there.
pause
exit /b 1

:found
if /i "%~1"=="--check" goto check
call "%ULTRAMIC_CONDA%" run --no-capture-output -n ultramic python -m ultramic_studio %*
if errorlevel 1 goto failed
exit /b 0

:check
call "%ULTRAMIC_CONDA%" run --no-capture-output -n ultramic python -m ultramic_studio.diagnostics --probe
if errorlevel 1 goto failed
echo Results saved to microphone_diagnostics.txt in this folder.
pause
exit /b 0

:failed
echo.
echo The app could not start. Copy the error above for troubleshooting.
echo This launcher uses your existing Anaconda environment named ultramic.
pause
exit /b 1
