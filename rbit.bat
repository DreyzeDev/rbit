@echo off
chcp 65001 >nul
set PYTHONSAFEPATH=1
set "RBIT_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%RBIT_PY%" set "RBIT_PY=python"
"%RBIT_PY%" -P -m rbit %*
exit /b %ERRORLEVEL%
