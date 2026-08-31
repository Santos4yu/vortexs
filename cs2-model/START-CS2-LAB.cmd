@echo off
title CS2 Prop Lab
cd /d "%~dp0"

if not exist "node_modules" (
  echo Preparing CS2 Prop Lab for first use...
  call npm install --no-audit --no-fund
  if errorlevel 1 (
    echo.
    echo Setup failed. Keep this window open and ask Codex for help.
    pause
    exit /b 1
  )
)

echo Starting CS2 Prop Lab...
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://localhost:3000'"
call npm run dev

echo.
echo CS2 Prop Lab stopped. Press any key to close.
pause >nul
