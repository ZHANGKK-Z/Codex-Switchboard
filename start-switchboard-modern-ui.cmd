@echo off
setlocal
set "PYTHON=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYTHON%" (
  echo [Codex Switchboard] Python virtual environment is missing.
  echo Please run:
  echo   python -m venv .venv
  echo   .\.venv\Scripts\pip install -r requirements-qt.txt
  echo.
  pause
  exit /b 1
)
start "Codex Switchboard" "%PYTHON%" "%~dp0switchboard_modern_ui.py" %*
endlocal
