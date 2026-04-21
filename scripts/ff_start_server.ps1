# Start the Fire Forex web server in the foreground.
#
# Running it foreground means closing the terminal kills it cleanly --
# the chronic zombie-uvicorn problem comes from background/detached
# launches. If you need to stop it without closing the terminal,
# press Ctrl-C, or run scripts\ff_kill_server.ps1 from another shell.
#
# Usage from the repo root:
#   .\scripts\ff_start_server.ps1

& .\.venv\Scripts\python.exe run.py web
