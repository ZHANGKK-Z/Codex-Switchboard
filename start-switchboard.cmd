@echo off
setlocal
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  set "PYTHON=python"
)
"%PYTHON%" "%~dp0switchboard.py" %*
endlocal
