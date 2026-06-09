"""
Position-weight caps — pure functions.

A "weight" here means "fraction of bucket capital to deploy in this
symbol", in [0, 1]. The Kelly sizer produces raw weights; these helpers
clamp them.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal


def apply_per_symbol_cap(
    weights: Mapping[str, Decimal], cap: Decimal
) -> dict[str, Decimal]:
    """Clamp every weight at ``cap``. Pure.

    Args:
        weights: raw {symbol: weight}.
        cap: maximum allowed per symbol (e.g. Decimal("0.30")).
    """
    if cap <= 0:
        raise ValueError(f"cap must be > 0, got {cap}")
    return {sym: min(w, cap) for sym, w in weights.items()}


def apply_aggregate_cap(
    weights: Mapping[str, Decimal], cap: Decimal
) -> dict[str, Decimal]:
    """Scale weights down proportionally if their sum exceeds ``cap``. Pure.

    Args:
        weights: {symbol: weight}.
        cap: maximum allowed sum (e.g. Decimal("1.00")).

    If the sum is already at or below the cap, returns the input unchanged.
    """
    if cap <= 0:
        raise ValueError(f"cap must be > 0, got {cap}")
    total = sum(weights.values(), Decimal("0"))
    if total <= cap:
        return dict(weights)
    scale = cap / total
    return {sym: w * scale for sym, w in weights.items()}
