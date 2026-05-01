"""Dashboard home: positions + recent trades."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from src.core.db import session_scope
from src.core.models import Position, PositionSide, Trade

router = APIRouter()


@router.get("/")
def home(request: Request):
    with session_scope() as session:
        positions = list(
            session.execute(
                select(Position)
                .where(Position.side != PositionSide.FLAT)
                .order_by(Position.updated_at.desc())
            ).scalars()
        )
        pos_data = [
            {
                "id": p.id,
                "strategy_id": p.strategy_id,
                "broker": p.broker.value,
                "symbol": p.symbol,
                "side": p.side.value,
                "quantity": str(p.quantity),
                "entry_price": str(p.entry_price) if p.entry_price else "—",
                "leverage": str(p.leverage) if p.leverage else "—",
                "liquidation_price": str(p.liquidation_price) if p.liquidation_price else "—",
            }
            for p in positions
        ]

        recent_trades = list(
            session.execute(
                select(Trade)
                .order_by(Trade.created_at.desc())
                .limit(50)
            ).scalars()
        )
        trade_data = [
            {
                "id": t.id,
                "strategy_id": t.strategy_id,
                "symbol": t.symbol,
                "side": t.side.value,
                "quantity": str(t.quantity),
                "price": str(t.price) if t.price else "—",
                "status": t.status.value,
                "submitted_at": (
                    t.submitted_at.strftime("%Y-%m-%d %H:%M") if t.submitted_at else "—"
                ),
            }
            for t in recent_trades
        ]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "positions": pos_data,
            "trades": trade_data,
        },
    )


@router.get("/partials/positions")
def positions_partial(request: Request):
    """HTMX partial: refreshes the positions table."""
    with session_scope() as session:
        positions = list(
            session.execute(
                select(Position)
                .where(Position.side != PositionSide.FLAT)
                .order_by(Position.updated_at.desc())
            ).scalars()
        )
        pos_data = [
            {
                "strategy_id": p.strategy_id,
                "broker": p.broker.value,
                "symbol": p.symbol,
                "side": p.side.value,
                "quantity": str(p.quantity),
                "entry_price": str(p.entry_price) if p.entry_price else "—",
                "leverage": str(p.leverage) if p.leverage else "—",
                "liquidation_price": str(p.liquidation_price) if p.liquidation_price else "—",
            }
            for p in positions
        ]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "partials/positions_table.html",
        {"request": request, "positions": pos_data},
    )


@router.get("/partials/trades")
def trades_partial(request: Request):
    """HTMX partial: refreshes the recent trades table."""
    with session_scope() as session:
        recent_trades = list(
            session.execute(
                select(Trade)
                .order_by(Trade.created_at.desc())
                .limit(50)
            ).scalars()
        )
        trade_data = [
            {
                "strategy_id": t.strategy_id,
                "symbol": t.symbol,
                "side": t.side.value,
                "quantity": str(t.quantity),
                "price": str(t.price) if t.price else "—",
                "status": t.status.value,
                "submitted_at": (
                    t.submitted_at.strftime("%Y-%m-%d %H:%M") if t.submitted_at else "—"
                ),
            }
            for t in recent_trades
        ]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "partials/trades_table.html",
        {"request": request, "trades": trade_data},
    )
