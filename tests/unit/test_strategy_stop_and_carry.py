"""Strategy-supplied ATR stops + MTF carry interest (Decision 032).

Two things swing-indian's 1h mean reversion needs that no earlier bucket did:

  * a protective stop placed at ``entry − 3.5 × daily ATR14`` rather than at the
    bucket's flat percent — the distance the backtest was validated with, and
    resting on the exchange so it holds while the bot is down;
  * financing cost on the funded portion of an MTF carry, which the backtest
    does not model (~4% of net) and the bot therefore books itself.
"""

from __future__ import annotations

from decimal import Decimal

from src.brokers.base import PositionInfo
from src.order_manager.pnl import carry_interest
from src.safety.stop_protection import (
    expected_trigger,
    expected_trigger_at_distance,
    plan_stop_protection,
)
from src.shared.bucket_runner import _entry_extra


def _pos(symbol: str, entry: str, size: str = "100") -> PositionInfo:
    return PositionInfo(
        symbol=symbol,
        side="long",
        size=Decimal(size),
        entry_price=Decimal(entry),
    )


def _plan(**kw):  # noqa: ANN003, ANN201
    base = {
        "positions": [_pos("MCX", "1000")],
        "open_orders": [],
        "stop_pct_by_bucket": {"swing-indian": Decimal("20")},
        "attribution": {"MCX": ("swing-indian", "mean_reversion_1h")},
    }
    return plan_stop_protection(**{**base, **kw})


# ---------------------------------------------------------------------------
# Trigger maths
# ---------------------------------------------------------------------------
def test_absolute_distance_trigger() -> None:
    # 3.5 × ATR(30) = 105 below a 1000 entry
    assert expected_trigger_at_distance(
        Decimal("1000"), "long", Decimal("105")
    ) == Decimal("895")
    assert expected_trigger_at_distance(
        Decimal("1000"), "short", Decimal("105")
    ) == Decimal("1105")


def test_atr_distance_replaces_the_bucket_percent() -> None:
    plan = _plan(stop_distances={"MCX": Decimal("105")})
    assert len(plan.place) == 1
    assert plan.place[0].trigger == Decimal("895")   # not 800 (the 20% net)


def test_no_distance_falls_back_to_the_bucket_percent() -> None:
    plan = _plan()
    assert plan.place[0].trigger == expected_trigger(
        Decimal("1000"), "long", Decimal("20")
    )
    assert plan.place[0].trigger == Decimal("800")


def test_a_wider_distance_than_the_crash_net_is_ignored() -> None:
    """The bucket percent is the guaranteed worst case; a bad ATR only tightens.

    A 400-rupee distance on a 1000 entry would sit at 600 — below the 20% net at
    800 — so it is refused and the net stands.
    """
    plan = _plan(stop_distances={"MCX": Decimal("400")})
    assert plan.place[0].trigger == Decimal("800")


def test_a_nonsensical_distance_is_ignored() -> None:
    plan = _plan(stop_distances={"MCX": Decimal("0")})
    assert plan.place[0].trigger == Decimal("800")


def test_an_existing_stop_at_the_atr_trigger_is_kept() -> None:
    """Idempotence: the sweep must not cancel/re-place a correct ATR stop."""
    from src.brokers.base import OpenOrder

    resting = OpenOrder(
        exchange_order_id="1",
        client_order_id=None,
        symbol="MCX",
        side="sell",
        size=Decimal("100"),
        unfilled_size=Decimal("100"),
        order_type="market_order",
        limit_price=None,
        status="open",
        stop_price=Decimal("895"),
        reduce_only=True,
    )
    plan = _plan(open_orders=[resting], stop_distances={"MCX": Decimal("105")})
    assert plan.place == []
    assert plan.cancel == []


# ---------------------------------------------------------------------------
# What the runner stamps on the entry order
# ---------------------------------------------------------------------------
def test_entry_extra_carries_stop_distance_and_margin() -> None:
    extra = _entry_extra(
        hint={"signal": "meanrev_1h_fresh_cross", "stop_distance": "105.5"},
        margin_inr=Decimal("10000"),
    )
    assert extra == {
        "margin_inr": "10000",
        "stop_distance": "105.5",
        "signal": "meanrev_1h_fresh_cross",
    }


def test_entry_extra_without_a_stop_distance() -> None:
    """A strategy that owns no stop still stamps the margin for the carry math."""
    extra = _entry_extra(hint={}, margin_inr=Decimal("10000"))
    assert extra == {"margin_inr": "10000"}


# ---------------------------------------------------------------------------
# MTF carry interest
# ---------------------------------------------------------------------------
def test_carry_interest_charges_only_the_funded_portion() -> None:
    # ₹38k notional on ₹10k of own capital → ₹28k funded, 14.6%/yr, 3 days
    charge = carry_interest(
        notional=Decimal("38000"),
        margin=Decimal("10000"),
        annual_rate=Decimal("0.146"),
        days=3,
    )
    assert charge == Decimal("28000") * Decimal("0.146") * 3 / Decimal("365")
    assert round(charge, 2) == Decimal("33.60")


def test_carry_interest_is_zero_without_a_rate() -> None:
    """Unfunded products (CNC, MIS, crypto) configure no rate and pay nothing."""
    assert carry_interest(
        notional=Decimal("38000"), margin=Decimal("10000"),
        annual_rate=None, days=3,
    ) == Decimal("0")


def test_carry_interest_is_zero_same_day_or_unfunded() -> None:
    assert carry_interest(
        notional=Decimal("38000"), margin=Decimal("10000"),
        annual_rate=Decimal("0.146"), days=0,
    ) == Decimal("0")
    # 1x CNC fallback: notional == margin, nothing is borrowed.
    assert carry_interest(
        notional=Decimal("10000"), margin=Decimal("10000"),
        annual_rate=Decimal("0.146"), days=5,
    ) == Decimal("0")


def test_carry_interest_is_zero_when_margin_was_never_stamped() -> None:
    """Entries placed before this shipped have no margin_inr — charge nothing
    rather than guessing a funded portion."""
    assert carry_interest(
        notional=Decimal("38000"), margin=None,
        annual_rate=Decimal("0.146"), days=3,
    ) == Decimal("0")


def test_carry_interest_over_the_backtest_window_is_about_four_percent() -> None:
    """Sanity vs the backtest_ref: ~₹6.5k on ₹164k of net over 214 trades.

    Average trade: ₹37,940 notional on ₹10,000 margin, held 2.68 calendar days.
    """
    per_trade = carry_interest(
        notional=Decimal("37940"),
        margin=Decimal("10000"),
        annual_rate=Decimal("0.146"),
        days=3,   # 2.68 rounds to whole days held
    )
    total = per_trade * 214
    assert Decimal("5000") < total < Decimal("8000")


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------
def test_swing_indian_ticks_every_minute() -> None:
    """A 1h strategy must not be paced at the 1d regime model's 15-minute tick."""
    from src.shared.bucket import load_bucket

    assert load_bucket("swing-indian").config.tick_interval_seconds == 60


def test_cadence_falls_back_to_the_fastest_timeframe() -> None:
    """Without an override, the bucket paces to its FASTEST tf, not the regime's."""
    from src.shared.bucket_runner import tick_interval_for_tf

    assert min(tick_interval_for_tf("1d"), tick_interval_for_tf("1h")) == 180
    assert tick_interval_for_tf("1d") == 900
