@echo off
REM Fire Forex dashboard launcher.
REM Starts local FastAPI server then opens browser.

cd /d "%~dp0"

REM Start server minimized so closing this window also stops server.
start "Fire Forex server" /MIN ".venv\Scripts\python.exe" run.py web

REM Wait briefly for server to bind.
powershell -NoProfile -Command "Start-Sleep -Milliseconds 1500"

REM Open dashboard in default browser.
start "" "http://127.0.0.1:8000/"
