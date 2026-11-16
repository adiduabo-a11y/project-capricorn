@echo off
setlocal
cd /d "%~dp0"
title Project Capricorn

if /I "%~1"=="--check" (
  echo root=%~dp0
  echo command=npm run dev
  exit /b 0
)

echo Starting Project Capricorn from %~dp0
echo The core starts automatically; leave this window open while Project Capricorn runs.
echo.
call npm run dev
set "code=%errorlevel%"
if not "%code%"=="0" (
  echo.
  echo Project Capricorn exited with code %code%.
  pause
)
exit /b %code%
