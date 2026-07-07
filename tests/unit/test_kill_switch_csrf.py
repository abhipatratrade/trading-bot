"""CSRF guard on the kill-switch toggle (Phase 1c)."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.dashboard.app import create_app


def _auth_headers() -> dict[str, str]:
    """Basic-auth header when the local env has a dashboard password."""
    settings = get_settings()
    if settings.dashboard_password is None:
        return {}
    raw = f"{settings.dashboard_user}:{settings.dashboard_password.get_secret_value()}"
    return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}


def test_toggle_without_token_is_403() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/kill-switch/toggle",
        data={"scope": "global", "action": "engage"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 403


def test_toggle_with_wrong_token_is_403() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/kill-switch/toggle",
        data={
            "scope": "global",
            "action": "engage",
            "csrf_token": "not-the-token",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 403


def test_token_is_embedded_in_template_globals() -> None:
    app = create_app()
    assert app.state.templates.env.globals["csrf_token"] == app.state.csrf_token
    assert len(app.state.csrf_token) >= 32
