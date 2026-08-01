"""Dashboard home: positions + recent trades."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from src.core.db import session_scope
from src.core.models import Position, PositionSide, Trade
from src.dashboard.routes.buckets import _trade_row

router = APIRouter()


@router.get("/")
def home(request: Request):
    with session_scope() as session:
        positions = list(
            session.execute(
                select(Position)
                .where(
                    Position.side != PositionSide.FLAT,
                    # Decision 027/033: only the bot's own positions. On the
                    # shared Dhan account an orphan-imported row (bucket_id
                    # NULL, as rows 244/245 were) would otherwise render here
                    # as if the bot opened it.
                    Position.bucket_id.is_not(None),
                )
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
        trade_data = [_trade_row(t) for t in recent_trades]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "home.html",
        {"positions": pos_data, "trades": trade_data},
    )


@router.get("/partials/positions")
def positions_partial(request: Request):
    """HTMX partial: refreshes the positions table."""
    with session_scope() as session:
        positions = list(
            session.execute(
                select(Position)
                .where(
                    Position.side != PositionSide.FLAT,
                    # Decision 027/033: only the bot's own positions. On the
                    # shared Dhan account an orphan-imported row (bucket_id
                    # NULL, as rows 244/245 were) would otherwise render here
                    # as if the bot opened it.
                    Position.bucket_id.is_not(None),
                )
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
        request,
        "partials/positions_table.html",
        {"positions": pos_data},
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
        trade_data = [_trade_row(t) for t in recent_trades]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/trades_table.html",
        {"trades": trade_data},
    )
