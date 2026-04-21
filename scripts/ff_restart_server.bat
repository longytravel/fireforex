@echo off
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0ff_restart_server.ps1" %*
