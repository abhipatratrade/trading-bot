"""
FastAPI + HTMX dashboard — read-only UI with kill-switch control.

All routes sit behind HTTP basic auth when ``DASHBOARD_PASSWORD`` is set
(Decision 021). Without it the app serves openly and logs a warning —
acceptable only while the service is not publicly reachable.

Run via ``src/entrypoints/run_dashboard.py`` or directly with uvicorn::

    uvicorn src.dashboard.app:create_app --factory --port 8000
"""

from __future__ import annotations

import base64
import binascii
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.core.config import get_settings
from src.core.logging import get_logger
from src.dashboard.routes import (
    buckets,
    export,
    journal,
    kill_switch,
    overview,
    params,
)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

_log = get_logger("dashboard.app")


def _num3(value: object) -> str:
    """Render Decimal/float/int with 3 decimal places. ``None`` → em-dash."""
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.3f}"
    except (ValueError, TypeError):
        return str(value)


def _check_basic_auth(header: str | None, user: str, password: str) -> bool:
    """Constant-time check of an ``Authorization: Basic ...`` header."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        given_user, _, given_pass = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user_ok = secrets.compare_digest(given_user.encode(), user.encode())
    pass_ok = secrets.compare_digest(given_pass.encode(), password.encode())
    return user_ok and pass_ok


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Bot Dashboard", docs_url=None, redoc_url=None)

    settings = get_settings()
    auth_user = settings.dashboard_user
    auth_password = (
        settings.dashboard_password.get_secret_value()
        if settings.dashboard_password
        else None
    )
    if auth_password is None:
        _log.warning(
            "dashboard_auth_disabled",
            note="set DASHBOARD_PASSWORD to enable basic auth",
        )

    @app.middleware("http")
    async def _basic_auth(request: Request, call_next):
        if auth_password is not None and not _check_basic_auth(
            request.headers.get("Authorization"), auth_user, auth_password
        ):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="trading-bot"'},
            )
        return await call_next(request)

    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    templates.env.filters["num3"] = _num3
    app.state.templates = templates
    # CSRF token for state-changing forms (kill-switch toggle). Random per
    # process: pages served by this process embed it; a cross-site POST
    # can't know it. Rotates on restart — a stale open tab just needs a
    # refresh after a redeploy.
    app.state.csrf_token = secrets.token_urlsafe(32)
    templates.env.globals["csrf_token"] = app.state.csrf_token

    @app.exception_handler(Exception)
    async def _global_error(request: Request, exc: Exception) -> HTMLResponse:
        # Never leak tracebacks/paths to the browser; log server-side.
        _log.error(
            "dashboard_unhandled_error",
            path=request.url.path,
            exc_info=exc,
        )
        return HTMLResponse(
            "<pre style='color:#f85149;background:#0d1117;padding:1rem'>"
            "Internal error — check the dashboard service logs.</pre>",
            status_code=500,
        )

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(overview.router)
    app.include_router(buckets.router)
    app.include_router(kill_switch.router)
    app.include_router(params.router)
    app.include_router(export.router)
    app.include_router(journal.router)

    return app
