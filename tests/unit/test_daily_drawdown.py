"""Daily-anchored drawdown breaker (Decision 023) — pure math, no I/O."""

from __future__ import annotations

from decimal import Decimal

from src.safety.breakers import check_daily_drawdown


def _run(
    anchor: str | None, current: str, threshold: str = "5"
) -> tuple[bool, dict]:
    res = check_daily_drawdown(
        anchor_equity=Decimal(anchor) if anchor is not None else None,
        current_equity=Decimal(current),
        max_drawdown_pct=Decimal(threshold),
    )
    assert res.name == "daily_drawdown"
    return res.tripped, res.detail


def test_no_loss_does_not_trip() -> None:
    tripped, detail = _run("1000", "1000")
    assert not tripped
    assert detail["drawdown_pct"] == "0"


def test_gain_does_not_trip() -> None:
    tripped, detail = _run("1000", "1100")
    assert not tripped
    assert detail["drawdown_pct"] == "0"


def test_loss_below_threshold_does_not_trip() -> None:
    tripped, _ = _run("1000", "960")  # -4% vs 5% threshold
    assert not tripped


def test_loss_at_threshold_trips() -> None:
    tripped, detail = _run("1000", "950")  # exactly -5%
    assert tripped
    assert Decimal(detail["drawdown_pct"]) == Decimal("5")


def test_loss_beyond_threshold_trips() -> None:
    tripped, _ = _run("1000", "800")
    assert tripped


def test_realized_loss_counts() -> None:
    # Wallet shrank (realized) with no open positions — old breaker
    # would have seen unrealized=0 and never tripped.
    tripped, _ = _run("1000", "900")
    assert tripped


def test_missing_anchor_skips() -> None:
    tripped, detail = _run(None, "900")
    assert not tripped
    assert detail["reason"] == "no_anchor"


def test_zero_anchor_skips() -> None:
    tripped, detail = _run("0", "0")
    assert not tripped
    assert detail["reason"] == "no_anchor"
