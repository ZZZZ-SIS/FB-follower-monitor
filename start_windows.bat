@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.10+ is required.
  pause
  exit /b 1
)

py -c "import PyQt6, playwright" >nul 2>nul
if errorlevel 1 (
  echo Required components are missing. Installing now...
  call install_windows.bat
)

py facebook_follower_tracker_v6.py
if errorlevel 1 pause
