@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.10+ is required.
  pause
  exit /b 1
)

py -m pip install --upgrade -r requirements-build.txt
if errorlevel 1 (
  pause
  exit /b 1
)

py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name FacebookFollowerTracker_v6_0 ^
  --add-data "assets\bell.wav;assets" ^
  --collect-all playwright ^
  facebook_follower_tracker_v6.py

if errorlevel 1 (
  echo [ERROR] Build failed.
  pause
  exit /b 1
)

echo.
echo EXE created:
echo %cd%\dist\FacebookFollowerTracker_v6_0.exe
pause
