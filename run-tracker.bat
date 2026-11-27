@echo off
rem Updates the tracker to the latest version and starts it. Double-click to run.
cd /d "%~dp0"

echo Getting the latest version...
git pull
if errorlevel 1 (
  echo.
  echo Could not update with git pull. Starting the version you already have.
  echo.
)

echo Installing...
python -m pip install --quiet --force-reinstall --no-deps .
if errorlevel 1 (
  pip install --quiet --force-reinstall --no-deps .
)

echo Starting the tracker. Dashboard: http://127.0.0.1:8765/
nbg-tracker run %*
pause
