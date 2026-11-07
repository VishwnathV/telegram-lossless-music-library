@echo off
setlocal
cd /d "%~dp0"
title Lossless Drive - Telegram Music Library
color 0B

echo.
echo  ============================================================
echo             LOSSLESS DRIVE - TELEGRAM MUSIC LIBRARY
echo  ============================================================
echo.

rem If the dashboard is already online, open it without starting a duplicate.
powershell.exe -NoProfile -Command "try { $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/' -TimeoutSec 2; if ($response.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo  Lossless Drive is already running. Opening the dashboard...
    start "" "http://127.0.0.1:8000"
    timeout /t 2 /nobreak >nul
    exit /b 0
)

rem Prefer the project virtual environment, then the Python launcher, then python.exe.
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
    set "PYTHON_VERSION_ARG="
) else (
    where py.exe >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=py.exe"
        set "PYTHON_VERSION_ARG=-3.11"
    ) else (
        where python.exe >nul 2>&1
        if errorlevel 1 goto :python_missing
        set "PYTHON_EXE=python.exe"
        set "PYTHON_VERSION_ARG="
    )
)

echo  Checking application dependencies...
"%PYTHON_EXE%" %PYTHON_VERSION_ARG% -c "import fastapi, telethon, mutagen, uvicorn" >nul 2>&1
if errorlevel 1 goto :dependencies_missing

set "PYTHONUTF8=1"
echo  Starting the local server at http://127.0.0.1:8000
echo  The dashboard will open automatically.
echo  Keep this window open while using Lossless Drive.
echo  Before closing, use "Stop safely" in the dashboard if sync is active.
echo  Then press Ctrl+C here to stop the local server.
echo.

start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8000'"
"%PYTHON_EXE%" %PYTHON_VERSION_ARG% -m music_library.cli serve --host 127.0.0.1 --port 8000

echo.
echo  Lossless Drive has stopped.
pause
exit /b 0

:python_missing
echo  ERROR: Python 3.11 or newer was not found.
echo  Install Python, then run this launcher again.
pause
exit /b 1

:dependencies_missing
echo  ERROR: The application dependencies are not installed.
echo.
echo  Open PowerShell in this folder and run:
echo      py -3.11 -m pip install -e ".[dev]"
echo.
pause
exit /b 1
