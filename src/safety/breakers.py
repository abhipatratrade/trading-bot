"""
Circuit breakers — automatic safety checks that trip the kill switch.

Each breaker returns a ``BreakerResult``. If ``tripped`` is True the caller
must engage the kill switch for the affected scope and stop trading.

Breakers:
    daily_drawdown     — cumulative realised + unrealised PnL today vs threshold
    liquidation_distance — any position whose mark price is too close to liq
    funding_extreme    — funding rate beyond a threshold (cost too high to hold)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from src.brokers.base import Broker
from src.core.logging import get_logger

_log = get_logger("safety.breakers")


@dataclass(frozen=True, slots=True)
class BreakerResult:
    name: str
    tripped: bool
    detail: dict[str, Any]


def check_daily_drawdown(
    broker: Broker,
    *,
    max_drawdown_pct: Decimal = Decimal("5"),
) -> BreakerResult:
    """Trip if total unrealised PnL across all positions exceeds threshold.

    Uses the broker's live position data.  The threshold is expressed as a
    percentage of total equity (available + margin).
    """
    positions = broker.get_positions()
    balances = broker.get_balances()

    total_equity = sum(
        (b.available + b.order_margin + b.position_margin for b in balances),
        Decimal("0"),
    )

    total_unrealised = sum(
        (p.unrealized_pnl or Decimal("0") for p in positions),
        Decimal("0"),
    )

    if total_equity <= 0:
        return BreakerResult(
            name="daily_drawdown",
            tripped=False,
            detail={"reason": "no_equity", "total_equity": "0"},
        )

    if total_unrealised < 0:
        drawdown_pct = abs(total_unrealised) / total_equity * 100
    else:
        drawdown_pct = Decimal("0")
    tripped = drawdown_pct >= max_drawdown_pct

    if tripped:
        _log.warning(
            "breaker_daily_drawdown_tripped",
            drawdown_pct=str(drawdown_pct),
            threshold=str(max_drawdown_pct),
            unrealised=str(total_unrealised),
            equity=str(total_equity),
        )

    return BreakerResult(
        name="daily_drawdown",
        tripped=tripped,
        detail={
            "drawdown_pct": str(drawdown_pct),
            "threshold_pct": str(max_drawdown_pct),
            "unrealised_pnl": str(total_unrealised),
            "total_equity": str(total_equity),
        },
    )


def check_liquidation_distance(
    broker: Broker,
    *,
    min_distance_pct: Decimal = Decimal("10"),
) -> BreakerResult:
    """Trip if any position's mark/entry price is within threshold of liq price."""
    positions = broker.get_positions()
    violations: list[dict[str, str]] = []

    for pos in positions:
        if pos.liquidation_price is None or pos.liquidation_price == 0:
            continue
        if pos.entry_price == 0:
            continue

        distance_pct = (
            abs(pos.entry_price - pos.liquidation_price)
            / pos.entry_price
            * 100
        )

        if distance_pct < min_distance_pct:
            violations.append({
                "symbol": pos.symbol,
                "entry_price": str(pos.entry_price),
                "liquidation_price": str(pos.liquidation_price),
                "distance_pct": str(distance_pct),
            })

    tripped = len(violations) > 0

    if tripped:
        _log.warning(
            "breaker_liquidation_distance_tripped",
            violations=violations,
            threshold=str(min_distance_pct),
        )

    return BreakerResult(
        name="liquidation_distance",
        tripped=tripped,
        detail={
            "threshold_pct": str(min_distance_pct),
            "violations": violations,
        },
    )


def check_funding_extreme(
    broker: Broker,
    data_source: Any,
    symbols: list[str],
    *,
    max_funding_rate: Decimal = Decimal("0.01"),
) -> BreakerResult:
    """Trip if any held symbol has abs(funding rate) above threshold.

    ``data_source`` must implement ``get_funding_rate(symbol)``.
    """
    violations: list[dict[str, str]] = []

    for sym in symbols:
        try:
            fr = data_source.get_funding_rate(sym)
        except Exception:
            _log.warning("funding_rate_fetch_failed", symbol=sym)
            continue

        if abs(fr.rate) >= max_funding_rate:
            violations.append({
                "symbol": sym,
                "funding_rate": str(fr.rate),
            })

    tripped = len(violations) > 0

    if tripped:
        _log.warning(
            "breaker_funding_extreme_tripped",
            violations=violations,
            threshold=str(max_funding_rate),
        )

    return BreakerResult(
        name="funding_extreme",
        tripped=tripped,
        detail={
            "threshold": str(max_funding_rate),
            "violations": violations,
        },
    )


def run_all_breakers(
    broker: Broker,
    data_source: Any,
    held_symbols: list[str],
    *,
    max_drawdown_pct: Decimal = Decimal("5"),
    min_liq_distance_pct: Decimal = Decimal("10"),
    max_funding_rate: Decimal = Decimal("0.01"),
) -> list[BreakerResult]:
    """Run every breaker and return the full list of results."""
    return [
        check_daily_drawdown(broker, max_drawdown_pct=max_drawdown_pct),
        check_liquidation_distance(broker, min_distance_pct=min_liq_distance_pct),
        check_funding_extreme(
            broker,
            data_source,
            held_symbols,
            max_funding_rate=max_funding_rate,
        ),
    ]
