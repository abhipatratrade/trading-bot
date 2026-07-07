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
from src.core.alerts import send_alert_dedup
from src.core.logging import get_logger

_log = get_logger("safety.breakers")

# Decision 024: breakers keep running while an account is killed, so a
# persistent condition would page every 60s tick — dedup-cap the detail
# alerts per breaker (enforcement.py sends the main dedup'd trip alert).


@dataclass(frozen=True, slots=True)
class BreakerResult:
    name: str
    tripped: bool
    detail: dict[str, Any]


def check_daily_drawdown(
    *,
    anchor_equity: Decimal | None,
    current_equity: Decimal,
    max_drawdown_pct: Decimal = Decimal("5"),
) -> BreakerResult:
    """Trip when equity has fallen ``max_drawdown_pct`` below the day anchor.

    Decision 023: ``anchor_equity`` is the account's start-of-UTC-day
    equity (``daily_equity_anchor`` row, written by the first breaker pass
    of the day). ``current_equity`` = wallet balance + unrealized PnL, so
    both realized losses through the day AND open drawdown count. Pure
    math — the caller (``safety.enforcement``) supplies both numbers.
    """
    if anchor_equity is None or anchor_equity <= 0:
        return BreakerResult(
            name="daily_drawdown",
            tripped=False,
            detail={
                "reason": "no_anchor",
                "anchor_equity": str(anchor_equity),
            },
        )

    loss = anchor_equity - current_equity
    drawdown_pct = (
        loss / anchor_equity * 100 if loss > 0 else Decimal("0")
    )
    tripped = drawdown_pct >= max_drawdown_pct

    if tripped:
        _log.warning(
            "breaker_daily_drawdown_tripped",
            drawdown_pct=str(drawdown_pct),
            threshold=str(max_drawdown_pct),
            anchor_equity=str(anchor_equity),
            current_equity=str(current_equity),
        )
        send_alert_dedup(
            "breaker_detail:daily_drawdown",
            f"BREAKER TRIPPED: daily_drawdown\n"
            f"drawdown={drawdown_pct:.2f}% (threshold {max_drawdown_pct}%)\n"
            f"equity={current_equity} vs day anchor={anchor_equity}",
        )

    return BreakerResult(
        name="daily_drawdown",
        tripped=tripped,
        detail={
            "drawdown_pct": str(drawdown_pct),
            "threshold_pct": str(max_drawdown_pct),
            "anchor_equity": str(anchor_equity),
            "current_equity": str(current_equity),
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
        send_alert_dedup(
            "breaker_detail:liquidation_distance",
            f"BREAKER TRIPPED: liquidation_distance (threshold {min_distance_pct}%)\n"
            + "\n".join(
                f"- {v['symbol']}: entry={v['entry_price']} "
                f"liq={v['liquidation_price']} dist={v['distance_pct']}%"
                for v in violations
            ),
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
        send_alert_dedup(
            "breaker_detail:funding_extreme",
            f"BREAKER TRIPPED: funding_extreme (threshold {max_funding_rate})\n"
            + "\n".join(
                f"- {v['symbol']}: rate={v['funding_rate']}"
                for v in violations
            ),
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
    anchor_equity: Decimal | None,
    current_equity: Decimal,
    max_drawdown_pct: Decimal = Decimal("5"),
    min_liq_distance_pct: Decimal = Decimal("10"),
    max_funding_rate: Decimal = Decimal("0.01"),
) -> list[BreakerResult]:
    """Run every breaker and return the full list of results."""
    return [
        check_daily_drawdown(
            anchor_equity=anchor_equity,
            current_equity=current_equity,
            max_drawdown_pct=max_drawdown_pct,
        ),
        check_liquidation_distance(broker, min_distance_pct=min_liq_distance_pct),
        check_funding_extreme(
            broker,
            data_source,
            held_symbols,
            max_funding_rate=max_funding_rate,
        ),
    ]
