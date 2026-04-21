@echo off
REM Wrapper for the ff-live-runner scheduled task. Replaces the direct
REM `python -m ff.live.runner_service` so stdout + stderr land in a log
REM file we can tail when the runner goes silent.
REM
REM Log path: artifacts\live\runner_stdout.log (overwritten each start)
REM
REM Intentional: no redirection append; each task /Run starts fresh.
setlocal
cd /d "C:\Projects\Fire Forex"
".venv\Scripts\python.exe" -u -m ff.live.runner_service > "artifacts\live\runner_stdout.log" 2>&1
