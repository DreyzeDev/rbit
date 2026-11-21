@echo off
chcp 65001 >nul
set "RBIT_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%RBIT_PY%" set "RBIT_PY=python"
rem `python -m rbit` puts the CURRENT directory first on sys.path, so a rbit\ folder or a
rem json.py inside the user's project would be imported instead of the real code. The
rem -P flag / PYTHONSAFEPATH only exist on Python 3.11+ (3.10 is supported), and the env var
rem would also leak into every Python process the agent starts. So drop '' / '.' from
rem sys.path here and run the package as __main__.
"%RBIT_PY%" -c "import sys, runpy; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; runpy.run_module('rbit', run_name='__main__', alter_sys=True)" %*
exit /b %ERRORLEVEL%
