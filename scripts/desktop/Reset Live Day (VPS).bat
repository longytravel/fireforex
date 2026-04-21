@echo off
REM Double-click on the VPS desktop to fully STOP trading and clear
REM state. Runner stays stopped until the laptop's Deploy button
REM kicks it back on with a fresh trial. Does the sequence:
REM   1. End the ff-live-runner Scheduled Task (trading OFF)
REM   2. git fetch + git reset --hard origin/main  (DISCARDS local edits)
REM   3. Flatten Fire Forex MT5 positions + archive plans/tickets/state
REM      + wipe any stale crashes.jsonl / errors.jsonl so the next
REM      Check run shows a clean slate
REM
REM Does NOT restart the runner. That is the laptop Deploy button's
REM job - no trades fire until the user explicitly redeploys.
REM
REM Everything under artifacts\live\ is archived under archive\<stamp>\
REM - nothing is destroyed. Local uncommitted code IS discarded; the
REM VPS should never hold edits so this is safe.

setlocal
cd /d "C:\Projects\Fire Forex"

echo.
echo === Fire Forex * full stop + reset (VPS) ===
echo.

echo This will:
echo   1. Stop ff-live-runner  (trading turns OFF)
echo   2. git reset --hard origin/main  (wipes any local VPS edits)
echo   3. Close every MT5 position tagged with the Fire Forex magic number
echo   4. Archive today's plans/tickets/state/crashes under artifacts\live\archive\
echo.
echo Runner stays STOPPED until you hit Deploy on the laptop UI.
echo.
echo Press Ctrl+C to abort, or any key to proceed.
pause >nul

echo.
echo --- 1/3 stopping runner ---
schtasks /End /TN ff-live-runner

echo.
echo --- 2/3 fetch + reset to origin/main ---
git fetch origin main
if errorlevel 1 (
    echo git fetch failed - aborting before we touch anything.
    pause
    exit /b 1
)
git reset --hard origin/main
if errorlevel 1 (
    echo git reset failed - aborting.
    pause
    exit /b 1
)

echo.
echo --- 3/3 reset live day ---
".venv\Scripts\python.exe" "scripts\reset_live_day.py"
set rc=%errorlevel%
if not "%rc%"=="0" (
    echo reset_live_day.py exited with errorlevel %rc% - inspect output above.
    pause
    exit /b %rc%
)

echo.
echo Done. Trading is OFF.
echo Next step: on the laptop, open the UI and hit Deploy on a backtest run.
echo That pushes a new service_config.json and kicks ff-live-runner.
echo.
echo Verify on VPS:
echo   * MT5 shows no Fire Forex positions
echo   * git log --oneline -1   matches the laptop HEAD
echo.
pause
