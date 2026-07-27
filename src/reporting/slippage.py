"""
Execution slippage — what the strategy saw vs what we actually got.

Both live strategies are ports of a backtest that fills at the OPEN of the bar
AFTER the signal bar. Live, we detect the signal moments after that bar closes
and send a market order. The two are *meant* to be equivalent, and the whole
question of whether the live edge matches the backtest turns on how equivalent
they really are.

Answering it needs three prices per trade, all stamped on the ``Trade`` row:

    signal_price    the close of the bar the strategy decided on — what it saw
    decision_price  the mark when the runner actually placed the order
    avg_fill_price  what the exchange gave us (stamped by the reconciler)

Which decomposes the gap into two costs with completely different fixes:

    decision lag = decision_price - signal_price
        The market moved between the bar closing and us acting. Caused by scan
        latency and tick cadence; fixed by making the loop faster.

    execution   = avg_fill_price - decision_price
        Spread and impact on a market order. Fixed by order type, or by
        filtering illiquid names out of the universe.

Reporting only the total would tell you that you are losing 30bps without
telling you which of those two to go and fix.

**Sign convention: positive is always a COST**, on both sides of the book. A
buy filled above its reference and a sell filled below it both come out
positive, so entries and exits can be averaged together without the two
cancelling into a comforting zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from src.core.models import OrderSide

_ZERO = Decimal("0")
_BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class Slippage:
    """The gap between decision and fill, split by cause. All in bps, cost-positive."""

    lag_bps: Decimal | None = None
    execution_bps: Decimal | None = None
    total_bps: Decimal | None = None

    @property
    def known(self) -> bool:
        return self.total_bps is not None


def to_decimal(value: object) -> Decimal | None:
    """Parse a JSONB-stored price string. None on anything unusable.

    Prices ride in ``Trade.extra`` as strings so the JSONB round-trips
    losslessly; a missing or malformed one must degrade to "unknown" rather
    than take down the report.
    """
    if value is None:
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out.is_finite() else None


def cost_bps(
    reference: Decimal | None,
    actual: Decimal | None,
    side: str | OrderSide,
) -> Decimal | None:
    """Signed cost of ``actual`` vs ``reference``, in bps. Positive = worse. PURE.

    Buying above the reference costs; selling below it costs. Returns None when
    either price is missing or the reference is not positive.
    """
    ref, act = to_decimal(reference), to_decimal(actual)
    if ref is None or act is None or ref <= 0:
        return None
    raw = (act - ref) / ref * _BPS
    sell = str(getattr(side, "value", side)).lower() == OrderSide.SELL.value
    return -raw if sell else raw


def decompose(
    *,
    signal_price: object,
    decision_price: object,
    fill_price: object,
    side: str | OrderSide,
) -> Slippage:
    """Split the signal→fill gap into lag and execution. PURE.

    Each leg is computed independently, so a trade missing one price still
    reports the other. ``total`` is measured signal→fill directly rather than
    summed, which keeps it correct when the middle price is absent.
    """
    return Slippage(
        lag_bps=cost_bps(signal_price, decision_price, side),
        execution_bps=cost_bps(decision_price, fill_price, side),
        total_bps=cost_bps(signal_price, fill_price, side),
    )


def mean_bps(values: list[Decimal | None]) -> Decimal | None:
    """Average of the known values. None when nothing is known. PURE."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    return sum(known, _ZERO) / Decimal(len(known))
