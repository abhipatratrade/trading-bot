"""CSV export endpoint for trades."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from src.core.db import session_scope
from src.core.models import Trade

router = APIRouter(prefix="/export", tags=["export"])


@router.get("/trades.csv")
def export_trades_csv(
    strategy_id: str | None = Query(None),
    limit: int = Query(1000, le=10000),
):
    """Stream trades as a CSV download."""
    with session_scope() as session:
        q = select(Trade).order_by(Trade.created_at.desc()).limit(limit)
        if strategy_id:
            q = q.where(Trade.strategy_id == strategy_id)
        trades = list(session.execute(q).scalars())

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "strategy_id", "broker", "symbol", "side",
            "quantity", "price", "fees", "leverage",
            "client_order_id", "exchange_order_id",
            "status", "submitted_at", "filled_at", "created_at",
        ])
        for t in trades:
            writer.writerow([
                t.id,
                t.strategy_id,
                t.broker.value,
                t.symbol,
                t.side.value,
                str(t.quantity),
                str(t.price) if t.price else "",
                str(t.fees),
                str(t.leverage) if t.leverage else "",
                t.client_order_id,
                t.exchange_order_id or "",
                t.status.value,
                t.submitted_at.isoformat() if t.submitted_at else "",
                t.filled_at.isoformat() if t.filled_at else "",
                t.created_at.isoformat() if t.created_at else "",
            ])

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    filename = f"trades_{ts}.csv"

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
