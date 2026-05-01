"""
FastAPI + HTMX dashboard — read-only UI with kill-switch control.

Run via ``src/entrypoints/run_dashboard.py`` or directly with uvicorn::

    uvicorn src.dashboard.app:create_app --factory --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.dashboard.routes import export, kill_switch, overview, params

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Bot Dashboard", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    app.state.templates = templates

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(overview.router)
    app.include_router(kill_switch.router)
    app.include_router(params.router)
    app.include_router(export.router)

    return app
