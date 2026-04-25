@echo off
setlocal
set REPO=%~dp0..\..
cd /d "%REPO%"
echo ============================================================
echo  Importing newest MT5 ReportHistory-*.html from your Desktop
echo ============================================================
echo.
".venv\Scripts\python.exe" scripts\import_mt5_report.py
echo.
echo ============================================================
echo  Done. CSV + JSON written to artifacts\live\incoming\
echo ============================================================
echo.
pause
endlocal
