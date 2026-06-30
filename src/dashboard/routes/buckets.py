"""
Bucket-tab routes — per (type × market) views.

URLs:
    GET  /buckets                — six-card overview grid
    GET  /buckets/{bucket_id}    — single bucket detail page
    POST /buckets/{bucket_id}/run — record a manual-run audit row

The "Start New Trade" button (PPTX slide 2) is implemented as an audit
row plus an alert: the bot service polls its own schedule and will pick
up the request on its next tick. We deliberately do NOT call BucketRunner
inside the dashboard process because the dashboard has no broker clients
(and would need a separate set of credentials).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select

from src.core.db import session_scope
from src.core.models import (
    AuditEventType,
    AuditLog,
    BucketState,
    Position,
    PositionSide,
    RegimeSnapshot,
    Trade,
)
from src.shared.bucket import load_buckets
from src.shared.strategy_loader import discover_strategies
from src.shared.strategy_master.loader import load_strategy_master

router = APIRouter(prefix="/buckets", tags=["buckets"])


# ---------------------------------------------------------------------------
# Overview grid
# ---------------------------------------------------------------------------
@router.get("")
def buckets_overview(request: Request):
    cards = _build_cards()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "buckets_overview.html",
        {"cards": cards},
    )


def _build_cards() -> list[dict[str, object]]:
    buckets = load_buckets()
    with session_scope() as session:
        states = {
            s.bucket_id: s
            for s in session.execute(select(BucketState)).scalars()
        }
        # Latest regime per bucket
        regimes: dict[str, RegimeSnapshot] = {}
        rows = session.execute(
            select(RegimeSnapshot).order_by(desc(RegimeSnapshot.ts))
        ).scalars()
        for r in rows:
            regimes.setdefault(r.bucket_id, r)
        # Open positions counts + total notional
        open_counts: dict[str, int] = {}
        for p in session.execute(
            select(Position).where(
                Position.side != PositionSide.FLAT,
                Position.quantity > 0,
            )
        ).scalars():
            bid = p.bucket_id or "(legacy)"
            open_counts[bid] = open_counts.get(bid, 0) + 1

    cards: list[dict[str, object]] = []
    for b in buckets:
        st = states.get(b.id)
        rg = regimes.get(b.id)
        cards.append(
            {
                "id": b.id,
                "trading_type": b.trading_type.value,
                "market": b.market.value,
                "broker": b.config.broker.value,
                "capital_inr": str(b.config.capital_inr),
                "available_inr": str(st.available_balance_inr) if st else "—",
                "locked_inr": str(st.locked_margin_inr) if st else "—",
                "leverage_max": str(b.config.leverage_max),
                "enabled": b.config.enabled,
                "open_positions": open_counts.get(b.id, 0),
                "regime": rg.regime.value if rg else None,
                "regime_age_min": _age_minutes(rg.ts) if rg else None,
                "regime_model_version": rg.model_version if rg else None,
            }
        )
    return cards


def _age_minutes(ts: datetime) -> int:
    return int((datetime.now(tz=timezone.utc) - ts).total_seconds() // 60)


# ---------------------------------------------------------------------------
# Bucket detail
# ---------------------------------------------------------------------------
@router.get("/{bucket_id}")
def bucket_detail(bucket_id: str, request: Request):
    bucket = _find_bucket(bucket_id)
    if bucket is None:
        raise HTTPException(status_code=404, detail=f"bucket {bucket_id} not found")

    # Strategy Master + discovered strategies → "scanning" list (eligible but flat)
    master = load_strategy_master(
        bucket.strategy_master_csv_path,
        bucket_trading_type=bucket.trading_type.value,
    )
    discovered = discover_strategies(bucket.strategies_folder)

    with session_scope() as session:
        # Running trades for this bucket
        positions = list(
            session.execute(
                select(Position).where(
                    Position.bucket_id == bucket_id,
                    Position.side != PositionSide.FLAT,
                    Position.quantity > 0,
                )
            ).scalars()
        )
        running = [
            {
                "symbol": p.symbol,
                "strategy_name": p.strategy_name or "—",
                "side": p.side.value,
                "quantity": str(p.quantity),
                "entry_price": str(p.entry_price) if p.entry_price else "—",
                "leverage": str(p.leverage) if p.leverage else "—",
                "opened_at": (
                    p.opened_at.strftime("%Y-%m-%d %H:%M")
                    if p.opened_at
                    else "—"
                ),
            }
            for p in positions
        ]
        held_symbols = {p.strategy_name for p in positions if p.strategy_name}

        # Recent trades (last 20)
        trades = list(
            session.execute(
                select(Trade)
                .where(Trade.bucket_id == bucket_id)
                .order_by(desc(Trade.created_at))
                .limit(20)
            ).scalars()
        )
        recent_trades = [
            {
                "strategy_name": t.strategy_name or "—",
                "symbol": t.symbol,
                "side": t.side.value,
                "quantity": str(t.quantity),
                "price": str(t.price) if t.price else "—",
                "status": t.status.value,
                "submitted_at": (
                    t.submitted_at.strftime("%Y-%m-%d %H:%M")
                    if t.submitted_at
                    else "—"
                ),
            }
            for t in trades
        ]

        # Bucket state + per-symbol regime (one row per (bucket, symbol)).
        # We pull the most-recent snapshot per symbol, ordered by ts desc.
        state = session.execute(
            select(BucketState).where(BucketState.bucket_id == bucket_id)
        ).scalar_one_or_none()
        regime_rows = session.execute(
            select(RegimeSnapshot)
            .where(RegimeSnapshot.bucket_id == bucket_id)
            .order_by(desc(RegimeSnapshot.ts))
        ).scalars().all()
        regimes_by_symbol: dict[str, RegimeSnapshot] = {}
        for r in regime_rows:
            regimes_by_symbol.setdefault(r.symbol, r)

    scanning: list[dict[str, object]] = []
    for name in discovered:
        row = master.by_name.get(name)
        if row is None:
            continue
        scanning.append(
            {
                "name": name,
                "tf": row.tf,
                "regime_gate": sorted(
                    [r.value for r in row.allowed_regimes]
                ) or ["(any)"],
                "live": name in held_symbols,
            }
        )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "bucket.html",
        {
            "bucket": {
                "id": bucket.id,
                "trading_type": bucket.trading_type.value,
                "market": bucket.market.value,
                "broker": bucket.config.broker.value,
                "enabled": bucket.config.enabled,
                "leverage_max": str(bucket.config.leverage_max),
                "capital_inr": str(bucket.config.capital_inr),
            },
            "state": {
                "available_inr": str(state.available_balance_inr) if state else "—",
                "locked_inr": str(state.locked_margin_inr) if state else "—",
            },
            "regimes": _regimes_table(regimes_by_symbol),
            "running": running,
            "scanning": scanning,
            "recent_trades": recent_trades,
        },
    )


# ---------------------------------------------------------------------------
# Manual run request
# ---------------------------------------------------------------------------
@router.post("/{bucket_id}/run")
def request_run(bucket_id: str):
    bucket = _find_bucket(bucket_id)
    if bucket is None:
        raise HTTPException(status_code=404, detail=f"bucket {bucket_id} not found")
    with session_scope() as session:
        session.add(
            AuditLog(
                strategy_id=bucket_id,
                event_type=AuditEventType.SCANNER_RUN,  # reused; manual-trigger flag in payload
                message=f"manual Start-New-Trade request for {bucket_id}",
                payload={
                    "bucket_id": bucket_id,
                    "manual_trigger": True,
                },
            )
        )
    return RedirectResponse(url=f"/buckets/{bucket_id}", status_code=303)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_bucket(bucket_id: str):
    for b in load_buckets():
        if b.id == bucket_id:
            return b
    return None


def _regimes_table(
    regimes_by_symbol: dict[str, RegimeSnapshot],
) -> list[dict[str, object]]:
    """Sort regime snapshots into a stable view: market row first, then
    per-coin rows alphabetised."""
    from src.shared.regime.store import MARKET_SENTINEL

    rows: list[dict[str, object]] = []
    market = regimes_by_symbol.get(MARKET_SENTINEL)
    if market is not None:
        rows.append(_regime_row(market, is_market=True))
    for sym in sorted(s for s in regimes_by_symbol if s != MARKET_SENTINEL):
        rows.append(_regime_row(regimes_by_symbol[sym], is_market=False))
    return rows


def _regime_row(snap: RegimeSnapshot, *, is_market: bool) -> dict[str, object]:
    return {
        "symbol": "(market)" if is_market else snap.symbol,
        "is_market": is_market,
        "label": snap.regime.value,
        "model_version": snap.model_version,
        "age_min": _age_minutes(snap.ts),
        "probabilities": snap.state_probabilities,
        # Continuous conviction P(bull)−P(bear) ∈ [-1, 1]; None on older rows.
        "signal": snap.signal,
    }


# Decimal import kept for future P/L calculations on the running-trades table.
_ = Decimal
_ = timedelta
