"""
FastAPI + HTMX dashboard — read-only UI with kill-switch control.

Run via ``src/entrypoints/run_dashboard.py`` or directly with uvicorn::

    uvicorn src.dashboard.app:create_app --factory --port 8000
"""

from __future__ import annotations

import traceback
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.dashboard.routes import buckets, export, kill_switch, overview, params

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def _num3(value: object) -> str:
    """Render Decimal/float/int with 3 decimal places. ``None`` → em-dash."""
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.3f}"
    except (ValueError, TypeError):
        return str(value)


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Bot Dashboard", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    templates.env.filters["num3"] = _num3
    app.state.templates = templates

    @app.exception_handler(Exception)
    async def _global_error(request: Request, exc: Exception) -> HTMLResponse:
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        return HTMLResponse(
            f"<pre style='color:#f85149;background:#0d1117;padding:1rem'>{''.join(tb)}</pre>",
            status_code=500,
        )

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(overview.router)
    app.include_router(buckets.router)
    app.include_router(kill_switch.router)
    app.include_router(params.router)
    app.include_router(export.router)

    return app
