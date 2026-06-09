"""
Kelly criterion — pure math.

For continuous returns (the right form for crypto), the Kelly-optimal
fraction of capital is:

    f* = μ / σ²

where μ is the per-period expected log return and σ² is its variance.
This is the unconstrained, infinite-bankroll formula. Real portfolios
use a fractional multiplier (typically 0.25) and a per-symbol cap.

This module is import-side-effect-free and has no I/O. Tests can call
``kelly_fraction`` directly with literal Decimals.
"""

from __future__ import annotations

from decimal import Decimal


def kelly_fraction(mu: Decimal, sigma: Decimal) -> Decimal:
    """Return f* = μ / σ², clamped at 0 for non-positive edge.

    Args:
        mu: expected per-period log return (e.g. mean daily log return).
        sigma: per-period standard deviation of log returns. Must be > 0.

    Returns:
        Decimal in [0, ∞). A 0 means "no edge — do not trade".

    Raises:
        ValueError: if sigma <= 0 (Kelly is undefined).
    """
    if sigma <= 0:
        raise ValueError(f"sigma must be > 0, got {sigma}")
    if mu <= 0:
        # Negative or zero edge → Kelly says "don't trade" in a long-only
        # context. (The full formula would say "go short" for μ < 0, but
        # Phase 1 is long-only — see Decision 015.)
        return Decimal("0")
    variance = sigma * sigma
    return mu / variance


def fractional_kelly(
    full: Decimal, fraction: Decimal = Decimal("0.25")
) -> Decimal:
    """Scale Kelly by a fixed fraction (default 0.25). Pure."""
    if fraction < 0:
        raise ValueError(f"fraction must be ≥ 0, got {fraction}")
    return full * fraction
