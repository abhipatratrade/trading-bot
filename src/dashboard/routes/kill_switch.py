"""Kill-switch page: view status + toggle ON/OFF."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import select

from src.core.db import session_scope
from src.core.models import KillSwitch
from src.safety.kill_switch import disengage, engage

router = APIRouter(prefix="/kill-switch", tags=["kill-switch"])


@router.get("")
def kill_switch_page(request: Request):
    rows = _get_kill_switch_rows()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "kill_switch.html",
        {"switches": rows},
    )


@router.post("/toggle")
def toggle_kill_switch(
    request: Request,
    scope: str = Form(...),
    strategy_id: str = Form(""),
    action: str = Form(...),
    reason: str = Form(""),
    csrf_token: str = Form(""),
):
    """Toggle a kill switch.  Called via HTMX from the dashboard."""
    # CSRF guard (Phase 1c): the token is embedded in every form this
    # process serves; a cross-site POST (riding cached basic-auth
    # credentials) can't know it. Constant-time compare.
    if not secrets.compare_digest(
        csrf_token, str(request.app.state.csrf_token)
    ):
        raise HTTPException(
            status_code=403,
            detail="CSRF token invalid — refresh the page and retry",
        )
    sid = strategy_id.strip() or None

    if action == "engage":
        engage(
            reason=reason or "Dashboard toggle",
            strategy_id=sid,
            engaged_by="dashboard",
        )
    elif action == "disengage":
        disengage(strategy_id=sid, disengaged_by="dashboard")

    rows = _get_kill_switch_rows()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/kill_switch_table.html",
        {"switches": rows},
    )


def _get_kill_switch_rows() -> list[dict]:
    with session_scope() as session:
        switches = list(
            session.execute(
                select(KillSwitch).order_by(KillSwitch.scope)
            ).scalars()
        )
        return [
            {
                "id": s.id,
                "scope": s.scope.value,
                "strategy_id": s.strategy_id or "—",
                "engaged": s.engaged,
                "reason": s.reason or "",
                "engaged_by": s.engaged_by or "",
                "engaged_at": (
                    s.engaged_at.strftime("%Y-%m-%d %H:%M") if s.engaged_at else "—"
                ),
            }
            for s in switches
        ]
