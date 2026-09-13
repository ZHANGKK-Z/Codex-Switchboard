@echo off
setlocal
set "PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" (
  set "PYTHONW=pythonw.exe"
)
start "Codex Switchboard" "%PYTHONW%" "%~dp0switchboard_ui.py" %*
endlocal
