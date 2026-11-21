$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
# `python -m rbit` puts the CURRENT directory first on sys.path, so a rbit\ folder or a
# json.py inside the user's project would be imported instead of the real code. The -P flag /
# PYTHONSAFEPATH only exist on Python 3.11+ (3.10 is supported), and the env var would also
# leak into every Python process the agent starts. So drop '' / '.' from sys.path here and
# run the package as __main__.
$boot = "import sys, runpy; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; runpy.run_module('rbit', run_name='__main__', alter_sys=True)"
& $python -c $boot @args
exit $LASTEXITCODE
