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

from datetime import UTC, datetime
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
from src.order_manager.pnl import (
    bucket_cumulative_pnl,
    bucket_ledger_pnl,
    realized_totals,
)
from src.shared.allocator.sizer import load_allocator_config
from src.shared.bucket import Bucket, Market, load_buckets
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
        pnl_amt, pnl_pct = _bucket_pnl(b, st)
        cards.append(
            {
                "id": b.id,
                "trading_type": b.trading_type.value,
                "market": b.market.value,
                "broker": b.config.broker.value,
                "capital_inr": str(b.config.capital_inr),
                "available_inr": str(st.available_balance_inr) if st else "—",
                "locked_inr": str(st.locked_margin_inr) if st else "—",
                "pnl_amt": pnl_amt,
                "pnl_pct": pnl_pct,
                "leverage_max": str(b.config.leverage_max),
                "enabled": b.config.enabled,
                "open_positions": open_counts.get(b.id, 0),
                "regime": rg.regime.value if rg else None,
                "regime_age_min": _age_minutes(rg.ts) if rg else None,
                "regime_model_version": rg.model_version if rg else None,
            }
        )
    return cards


def _trade_ledger_pnl(bucket_id: str) -> tuple[Decimal, Decimal]:
    """``(realized, unrealized)`` from this bucket's own Trade rows.

    Realized stamps land on BOTH legs of a round-trip, so only the reduce-only
    (exit) leg is counted. Unrealized is stamped on entry legs still open on
    the exchange (reconciler pass 3).
    """
    realized = Decimal("0")
    unrealized = Decimal("0")
    with session_scope() as session:
        for t in session.execute(
            select(Trade).where(Trade.bucket_id == bucket_id)
        ).scalars():
            e = t.extra or {}
            raw = e.get("pnl_usd")
            if raw is None:
                continue
            try:
                val = Decimal(str(raw))
            except ArithmeticError:
                continue
            if e.get("pnl_kind") == "realized" and e.get("reduce_only"):
                realized += val
            elif e.get("pnl_kind") == "unrealized" and not e.get("reduce_only"):
                unrealized += val
    return realized, unrealized


def _bucket_pnl(
    bucket: Bucket, state: BucketState | None
) -> tuple[float | None, float | None]:
    """Cumulative bot P&L for a bucket (floats for template rendering).

    Crypto: wallet equity (synced by the reconciler, Decision 021) minus the
    capital seed, adjusted by any manual deposits/withdrawals recorded in
    ``bucket_state.extra["capital_adjustments_inr"]``. Sound because Decision
    019 gives each crypto bucket its own sub-account, so wallet == bucket.

    Indian: Dhan has no sub-accounts, so several buckets mirror the SAME
    wallet and equity cannot be attributed that way (Decision 029). P&L comes
    from the bucket's own trade ledger instead — see ``bucket_ledger_pnl``.
    """
    if state is None:
        return None, None
    adjustments = Decimal("0")
    raw_adj = (state.extra or {}).get("capital_adjustments_inr")
    if raw_adj is not None:
        try:
            adjustments = Decimal(str(raw_adj))
        except ArithmeticError:
            pass
    if bucket.market == Market.INDIAN:
        realized, unrealized = _trade_ledger_pnl(bucket.id)
        amt, pct = bucket_ledger_pnl(
            capital=bucket.config.capital_inr,
            realized=realized,
            unrealized=unrealized,
            adjustments=adjustments,
        )
    else:
        amt, pct = bucket_cumulative_pnl(
            capital=bucket.config.capital_inr,
            available=state.available_balance_inr,
            locked=state.locked_margin_inr,
            adjustments=adjustments,
        )
    return float(amt), (float(pct) if pct is not None else None)


def _age_minutes(ts: datetime) -> int:
    return int((datetime.now(tz=UTC) - ts).total_seconds() // 60)


def _money_stats(bucket: Bucket, state: BucketState | None) -> dict[str, float | None]:
    """The five header numbers, all in INR at the bucket's fixed fx.

    - deposited / withdrawn: lifetime wallet flows cached by the
      reconciler in ``bucket_state.extra`` (USD, from the exchange's
      transaction history).
    - profit / loss: realized round-trips only — reduce-only exit trades
      whose P&L enrichment stamped ``pnl_kind == "realized"``. Winners
      and losers summed separately.
    - fees: Σ ``Trade.fees`` for the bucket.
    """
    try:
        fx = load_allocator_config(bucket.allocator_yaml_path).fx_inr_per_usd
    except Exception:
        fx = Decimal("1")

    extra = (state.extra or {}) if state else {}

    def _inr(key: str) -> float | None:
        raw = extra.get(key)
        if raw is None:
            return None
        try:
            return float(Decimal(str(raw)) * fx)
        except ArithmeticError:
            return None

    pnls: list[Decimal] = []
    fees = Decimal("0")
    with session_scope() as session:
        trades = session.execute(
            select(Trade).where(Trade.bucket_id == bucket.id)
        ).scalars()
        for t in trades:
            if t.fees:
                fees += t.fees
            e = t.extra or {}
            # Realized stamps are copied onto BOTH legs; count exits only.
            if (
                e.get("reduce_only")
                and e.get("pnl_kind") == "realized"
                and e.get("pnl_usd") is not None
            ):
                try:
                    pnls.append(Decimal(str(e["pnl_usd"])))
                except ArithmeticError:
                    pass

    profit, loss = realized_totals(pnls)
    return {
        "deposited_inr": _inr("wallet_deposits_usd"),
        "withdrawn_inr": _inr("wallet_withdrawals_usd"),
        "profit_inr": float(profit * fx),
        "loss_inr": float(loss * fx),
        "fees_inr": float(fees * fx),
    }


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
        recent_trades = [_trade_row(t) for t in trades]

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

    pnl_amt, pnl_pct = _bucket_pnl(bucket, state)
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
                "pnl_amt": pnl_amt,
                "pnl_pct": pnl_pct,
            },
            "money": _money_stats(bucket, state),
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


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (ValueError, TypeError):
        return None


def _trade_row(t: Trade) -> dict[str, object]:
    """Template dict for one trade row, incl. P&L fields written by the
    reconciler's fill-enrichment sweep (floats so templates can colour by
    sign; None until the first sweep after the fill)."""
    extra = t.extra or {}
    return {
        "strategy_name": t.strategy_name or "—",
        "strategy_id": t.strategy_id,
        "symbol": t.symbol,
        "side": t.side.value,
        "quantity": str(t.quantity),
        "price": (
            str(extra.get("avg_fill_price"))
            if extra.get("avg_fill_price")
            else (str(t.price) if t.price else "—")
        ),
        "status": t.status.value,
        "submitted_at": (
            t.submitted_at.strftime("%Y-%m-%d %H:%M") if t.submitted_at else "—"
        ),
        "traded_amt": _float_or_none(extra.get("traded_notional_usd")),
        "pnl_amt": _float_or_none(extra.get("pnl_usd")),
        "pnl_pct": _float_or_none(extra.get("pnl_pct")),
        "pnl_kind": extra.get("pnl_kind"),
    }
