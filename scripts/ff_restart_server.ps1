# Fire Forex — authoritative server restart.
#
# Kills any python.exe bound to :8000/:8001, clears stale __pycache__
# under app/ and ff/, then starts a fresh uvicorn. This is the ONLY
# supported way to start the web UI; see CLAUDE.md.
#
# Desktop shortcuts should target ff_restart_server.bat (the wrapper).

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

Write-Host "Killing listeners on :8000/:8001 ..."
Get-NetTCPConnection -LocalPort 8000,8001 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

Write-Host "Clearing __pycache__ under app\ and ff\ ..."
Get-ChildItem -Path (Join-Path $repo "app"), (Join-Path $repo "ff") `
              -Recurse -Force -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Starting Fire Forex web UI ..."
& (Join-Path $repo ".venv\Scripts\python.exe") (Join-Path $repo "run.py") web
