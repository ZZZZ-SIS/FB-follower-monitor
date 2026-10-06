@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo Facebook Follower Tracker v6.0 - Install
echo ============================================

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python launcher "py" not found.
  echo Please install Python 3.10 or newer first.
  pause
  exit /b 1
)

py -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Python package installation failed.
  pause
  exit /b 1
)

py -m playwright install chromium
if errorlevel 1 (
  echo [WARN] Playwright Chromium install failed.
  echo The app can still use installed Microsoft Edge or Google Chrome.
)

echo.
echo Installation complete.
pause
