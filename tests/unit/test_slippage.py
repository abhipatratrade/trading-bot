"""Slippage decomposition and live-edge stats — pure math, no I/O."""

from __future__ import annotations

from decimal import Decimal

from src.core.models import OrderSide
from src.order_manager.pnl import profit_factor, win_rate
from src.reporting.slippage import cost_bps, decompose, mean_bps, to_decimal


def D(x: str) -> Decimal:  # noqa: N802 - terse on purpose, this file is dense
    return Decimal(x)


# ---------------------------------------------------------------------------
# cost_bps — positive is always a cost, on both sides
# ---------------------------------------------------------------------------
def test_buying_above_the_reference_is_a_cost() -> None:
    assert cost_bps(D("100"), D("101"), OrderSide.BUY) == D("100")


def test_buying_below_the_reference_is_a_gain() -> None:
    assert cost_bps(D("100"), D("99"), OrderSide.BUY) == D("-100")


def test_selling_below_the_reference_is_a_cost() -> None:
    """The sign flips on a sell, so entries and exits can be averaged."""
    assert cost_bps(D("100"), D("99"), OrderSide.SELL) == D("100")


def test_selling_above_the_reference_is_a_gain() -> None:
    assert cost_bps(D("100"), D("101"), OrderSide.SELL) == D("-100")


def test_side_accepts_a_plain_string() -> None:
    assert cost_bps(D("100"), D("99"), "sell") == D("100")


def test_missing_prices_are_unknown_not_zero() -> None:
    """A trade placed before signal prices were recorded must not read as 0."""
    assert cost_bps(None, D("101"), OrderSide.BUY) is None
    assert cost_bps(D("100"), None, OrderSide.BUY) is None


def test_a_nonpositive_reference_is_unknown() -> None:
    assert cost_bps(D("0"), D("101"), OrderSide.BUY) is None
    assert cost_bps(D("-5"), D("101"), OrderSide.BUY) is None


def test_prices_parse_from_jsonb_strings() -> None:
    assert cost_bps("100", "101", OrderSide.BUY) == D("100")


def test_garbage_prices_are_unknown() -> None:
    assert to_decimal("not-a-price") is None
    assert to_decimal(None) is None
    assert to_decimal("NaN") is None
    assert to_decimal("Infinity") is None


# ---------------------------------------------------------------------------
# decompose
# ---------------------------------------------------------------------------
def test_decompose_splits_lag_from_execution() -> None:
    slip = decompose(
        signal_price="100",
        decision_price="100.5",   # market drifted up while we scanned
        fill_price="100.8",       # then spread cost us more
        side=OrderSide.BUY,
    )
    assert slip.lag_bps == D("50")
    assert slip.execution_bps is not None
    assert round(slip.execution_bps, 1) == D("29.9")
    assert slip.total_bps == D("80")
    assert slip.known


def test_total_is_measured_directly_not_summed() -> None:
    """So a missing middle price still yields a correct total."""
    slip = decompose(
        signal_price="100",
        decision_price=None,
        fill_price="101",
        side=OrderSide.BUY,
    )
    assert slip.lag_bps is None
    assert slip.execution_bps is None
    assert slip.total_bps == D("100")


def test_a_trade_with_no_prices_is_entirely_unknown() -> None:
    slip = decompose(
        signal_price=None, decision_price=None, fill_price=None, side="buy"
    )
    assert not slip.known


def test_exit_only_reports_execution() -> None:
    """Exits carry no signal_price — select_exits returns bare symbols."""
    slip = decompose(
        signal_price=None,
        decision_price="100",
        fill_price="99.5",
        side=OrderSide.SELL,
    )
    assert slip.execution_bps == D("50")  # sold below the mark = cost
    assert slip.lag_bps is None


# ---------------------------------------------------------------------------
# mean_bps
# ---------------------------------------------------------------------------
def test_mean_ignores_unknowns_rather_than_counting_them_as_zero() -> None:
    assert mean_bps([D("10"), None, D("20")]) == D("15")


def test_mean_of_nothing_known_is_unknown() -> None:
    assert mean_bps([None, None]) is None
    assert mean_bps([]) is None


# ---------------------------------------------------------------------------
# Live edge stats
# ---------------------------------------------------------------------------
def test_profit_factor_is_gross_profit_over_gross_loss() -> None:
    assert profit_factor([D("200"), D("-100")]) == D("2")


def test_profit_factor_with_no_losers_is_undefined_not_infinite() -> None:
    """Printing inf beside a backtest's 2.31 would read as spectacular."""
    assert profit_factor([D("200"), D("50")]) is None


def test_profit_factor_of_nothing_is_undefined() -> None:
    assert profit_factor([]) is None


def test_win_rate_counts_only_strict_winners() -> None:
    assert win_rate([D("10"), D("-5"), D("0"), D("7")]) == D("0.5")


def test_win_rate_of_nothing_is_undefined() -> None:
    assert win_rate([]) is None


def test_profit_factor_is_scale_invariant() -> None:
    """Which is what makes live rupees comparable to a backtest's returns."""
    rupees = [D("2000"), D("-1000")]
    returns = [D("0.02"), D("-0.01")]
    assert profit_factor(rupees) == profit_factor(returns)
