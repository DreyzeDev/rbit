$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
& "$PSScriptRoot\.venv\Scripts\python.exe" -m rbit @args
