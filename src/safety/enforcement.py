"""
Breaker enforcement — runs the safety checks and ACTS on trips.

Decision 021 semantics: when any breaker trips for a Delta sub-account,

    1. engage the kill switch for every bucket on that account, then
    2. flatten all open positions with reduce-only market orders
       (reduce-only orders are allowed through an engaged kill switch).

Recovery is manual: the user disengages the kill switch from the
dashboard once the underlying condition is understood.

``run_bot`` calls :func:`enforce_breakers` once per tick per sub-account.
Accounts whose buckets are all already killed are skipped, so a trip
fires the flatten exactly once and stays quiet afterwards.
"""

from __future__ import annotations

from decimal import Decimal

from src.brokers.base import Broker, OrderType, PositionInfo
from src.core.alerts import send_alert
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import AuditEventType, AuditLog
from src.order_manager.manager import OrderManager
from src.safety import kill_switch
from src.safety.breakers import run_all_breakers
from src.safety.equity_anchor import get_or_create_daily_anchor

_log = get_logger("safety.enforcement")


def enforce_breakers(
    *,
    account_ref: str,
    bucket_ids: list[str],
    broker: Broker,
    order_manager: OrderManager,
    data: object,
    max_drawdown_pct: Decimal,
    min_liq_distance_pct: Decimal,
    max_funding_rate: Decimal,
    clock: Clock | None = None,
) -> bool:
    """Run all breakers for one sub-account; halt + flatten on any trip.

    Returns True if a trip was handled this call, False otherwise.
    """
    clk = clock or RealClock()

    # Already halted → nothing to enforce (prevents re-flatten every tick).
    if all(kill_switch.is_engaged(b) for b in bucket_ids):
        return False

    positions = broker.get_positions()
    held_symbols = [p.symbol for p in positions]

    # Daily-anchored equity (Decision 023): wallet balance + unrealized PnL,
    # measured against the account's start-of-UTC-day anchor so realized
    # losses through the day count toward the drawdown, not just open PnL.
    balances = broker.get_balances()
    current_equity = sum(
        (b.available + b.order_margin + b.position_margin for b in balances),
        Decimal("0"),
    ) + sum(
        (p.unrealized_pnl or Decimal("0") for p in positions),
        Decimal("0"),
    )
    anchor_equity = get_or_create_daily_anchor(
        account_ref, current_equity, clk
    )

    results = run_all_breakers(
        broker,
        data,
        held_symbols,
        anchor_equity=anchor_equity,
        current_equity=current_equity,
        max_drawdown_pct=max_drawdown_pct,
        min_liq_distance_pct=min_liq_distance_pct,
        max_funding_rate=max_funding_rate,
    )
    tripped = [r for r in results if r.tripped]
    if not tripped:
        return False

    names = [r.name for r in tripped]
    _log.warning(
        "breakers_tripped_enforcing",
        account_ref=account_ref,
        buckets=bucket_ids,
        breakers=names,
    )

    with session_scope() as session:
        for r in tripped:
            session.add(
                AuditLog(
                    strategy_id=bucket_ids[0] if bucket_ids else None,
                    event_type=AuditEventType.BREAKER_TRIPPED,
                    message=f"Breaker {r.name} tripped on account {account_ref}",
                    payload={
                        "account_ref": account_ref,
                        "buckets": bucket_ids,
                        "breaker": r.name,
                        "detail": r.detail,
                    },
                )
            )

    # 1. Halt: per-bucket kill switch (dashboard-visible, manually cleared).
    reason = f"breaker(s) tripped: {', '.join(names)}"
    for bucket_id in bucket_ids:
        kill_switch.engage(reason, strategy_id=bucket_id, engaged_by="breaker")

    # 2. Flatten every open position on the account.
    flattened, failed = _flatten_positions(
        positions=positions,
        bucket_id=bucket_ids[0] if bucket_ids else "unknown",
        order_manager=order_manager,
        clock=clk,
    )

    send_alert(
        f"BREAKER ENFORCEMENT on {account_ref} (buckets {bucket_ids})\n"
        f"Tripped: {', '.join(names)}\n"
        f"Kill switch ENGAGED; flatten: {flattened} closed, {len(failed)} FAILED"
        + (f"\nFailed symbols: {failed} — CLOSE MANUALLY" if failed else "")
    )
    return True


def _flatten_positions(
    *,
    positions: list[PositionInfo],
    bucket_id: str,
    order_manager: OrderManager,
    clock: Clock,
) -> tuple[int, list[str]]:
    """Close every position with a reduce-only market order.

    Returns (count_closed, failed_symbols). Failures are collected, not
    raised — one stuck symbol must not stop the rest of the flatten.
    """
    flattened = 0
    failed: list[str] = []
    minute = clock.now().strftime("%Y%m%d%H%M")
    for pos in positions:
        side = "sell" if pos.side == "long" else "buy"
        try:
            order_manager.place_order(
                strategy_id=bucket_id,
                bucket_id=bucket_id,
                strategy_name="breaker_flatten",
                symbol=pos.symbol,
                side=side,
                size=pos.size,
                order_type=OrderType.MARKET,
                reduce_only=True,
                allow_when_killed=True,
                intent_id=f"breaker-flatten-{minute}",
            )
            flattened += 1
        except Exception:
            _log.error(
                "breaker_flatten_failed",
                symbol=pos.symbol,
                exc_info=True,
            )
            failed.append(pos.symbol)
    return flattened, failed
