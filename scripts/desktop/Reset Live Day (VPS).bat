@echo off
REM Double-click on the VPS desktop to pick up a laptop push + start a
REM fresh live trading day. Does the full sequence:
REM   1. End the ff-live-runner Scheduled Task
REM   2. git fetch + git reset --hard origin/main  (DISCARDS local edits)
REM   3. Flatten Fire Forex MT5 positions + archive today's plans/tickets/state
REM   4. Restart the Scheduled Task so it picks up new code
REM
REM Everything under artifacts\live\ is archived under archive\<stamp>\
REM — nothing is destroyed. Local uncommitted code IS discarded; the VPS
REM should never hold edits so this is safe.

setlocal
cd /d "C:\Projects\Fire Forex"

echo.
echo === Fire Forex · pull + reset live day (VPS) ===
echo.

echo This will:
echo   1. Stop ff-live-runner
echo   2. git reset --hard origin/main  (wipes any local VPS edits)
echo   3. Close every MT5 position tagged with the Fire Forex magic number
echo   4. Archive today's plans/tickets/state under artifacts\live\archive\
echo   5. Restart ff-live-runner
echo.
echo Press Ctrl+C to abort, or any key to proceed.
pause >nul

echo.
echo --- 1/4 stopping runner ---
schtasks /End /TN ff-live-runner

echo.
echo --- 2/4 fetch + reset to origin/main ---
git fetch origin main
if errorlevel 1 (
    echo git fetch failed — aborting before we touch anything.
    pause
    exit /b 1
)
git reset --hard origin/main
if errorlevel 1 (
    echo git reset failed — aborting.
    pause
    exit /b 1
)

echo.
echo --- 3/4 reset live day ---
".venv\Scripts\python.exe" "scripts\reset_live_day.py"
set rc=%errorlevel%
if not "%rc%"=="0" (
    echo reset_live_day.py exited with errorlevel %rc% — NOT restarting runner.
    pause
    exit /b %rc%
)

echo.
echo --- 4/4 starting runner ---
schtasks /Run /TN ff-live-runner

echo.
echo Done. Verify:
echo   * MT5 shows no Fire Forex positions
echo   * git log --oneline -1   matches the laptop HEAD
echo   * In ~60s: git log origin/live-state --oneline -3  shows fresh "state sync" commit
echo.
pause
