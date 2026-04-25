"""FastAPI app entry-point. Bind to 127.0.0.1 only.

Run with::

    uvicorn app.api:api --host 127.0.0.1 --port 8000

or via ``python run.py web``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import live_state_puller
from . import routes as app_routes

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"


api = FastAPI(title="Fire Forex", docs_url="/api/docs", openapi_url="/api/openapi.json")
api.include_router(app_routes.router)


# Background pull of VPS live-state branch. One daemon per process, kicked
# off on FastAPI startup — survives reloads (uvicorn --reload spawns a new
# process, new thread). Disable with FF_DISABLE_LIVE_STATE_PULL=1.
@api.on_event("startup")
def _kick_live_state_pull() -> None:
    live_state_puller.start_pull_thread(interval_sec=60)


# Convenience: serve the comparison dashboard under a short alias too.
@api.get("/comparison.html", include_in_schema=False)
def comparison_alias() -> FileResponse:
    path = ARTIFACTS_DIR / "comparison.html"
    if not path.exists():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")
    return FileResponse(path, media_type="text/html")


# Static frontend — served last so API routes win over /static/*.
if STATIC_DIR.exists():
    api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@api.get("/", include_in_schema=False)
def root() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index, media_type="text/html")
    return FileResponse(PROJECT_ROOT / "README.md", media_type="text/markdown")
