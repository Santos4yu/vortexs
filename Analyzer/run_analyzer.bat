@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

set "ANALYZER_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%ANALYZER_PY%" (
    echo First-time setup: creating the Analyzer's private Python environment...
    python -m venv "%~dp0.venv"
    if errorlevel 1 goto setup_failed
)

"%ANALYZER_PY%" -c "import requests, aiohttp, discord, dotenv" >nul 2>&1
if errorlevel 1 (
    echo Installing the Analyzer's required packages. This only happens once...
    "%ANALYZER_PY%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 goto setup_failed
)

"%ANALYZER_PY%" Analyzer\analyzer.py %*
echo.
pause
exit /b 0

:setup_failed
echo.
echo Analyzer setup could not finish. Check your internet connection, then run this file again.
echo If Python itself was not found, install Python and enable "Add Python to PATH".
echo.
pause
exit /b 1
