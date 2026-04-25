@echo off
setlocal
set "REPO=%~dp0..\.."
cd /d "%REPO%"
echo ============================================================
echo  Live MT5 status — open positions, pending orders, account
echo ============================================================
echo.
".venv\Scripts\python.exe" scripts\mt5_status.py --save
echo.
echo ============================================================
echo  Done. Snapshot also saved to artifacts\live\incoming\
echo ============================================================
echo.
pause
endlocal
