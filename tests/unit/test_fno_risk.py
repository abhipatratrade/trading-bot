"""F&O risk invariants and short ownership (Decision 036, Phase D).

Two of these guard hazards that existed in code already live on real money, and
would have gone unnoticed until an F&O bucket was switched on:

* ``test_a_naked_short_is_owned_not_foreign`` — ownership was long-only, so a
  short position nets negative and reads as somebody else's. The stop sweep
  skips it, the reconciler files it as the user's, the breaker flatten passes
  over it. An unbounded-loss position that no safety path believes it owns.
* ``test_a_short_is_covered_by_a_buy_stop_not_a_sell`` — the coverage check
  hardcoded "a protective stop is a SELL", true of every Indian position until
  the options bucket.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.brokers.base import OpenOrder, PositionInfo
from src.core.models import OrderSide, OrderStatus
from src.order_manager.ownership import net_owned, net_owned_signed
from src.safety.session_invariants import (
    Severity,
    check_expiry_window,
    check_margin_utilisation,
    check_stop_coverage,
    effective_holdings,
)

TODAY = date(2026, 9, 1)
NIFTY_CE = "NIFTY-20260908-24500-CE"   # 7 days out
RELIANCE_CE = "RELIANCE-20260902-1400-CE"  # 1 day out, stock => physical
INDEXES = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"})


class _T:
    """Minimal Trade stand-in — the ownership protocol is structural."""

    def __init__(self, symbol, side, qty, status=OrderStatus.FILLED, extra=None):
        self.symbol = symbol
        self.side = side
        self.quantity = Decimal(str(qty))
        self.status = status
        self.extra = extra or {}


def _pos(symbol, side, size, entry="100") -> PositionInfo:
    return PositionInfo(
        symbol=symbol, side=side, size=Decimal(str(size)),
        entry_price=Decimal(entry),
    )


def _order(symbol, side, size, stop="90", reduce_only=False) -> OpenOrder:
    return OpenOrder(
        exchange_order_id="1", client_order_id=None, symbol=symbol, side=side,
        size=Decimal(str(size)), unfilled_size=Decimal(str(size)),
        order_type="stop_market", limit_price=None, status="open",
        stop_price=Decimal(stop), reduce_only=reduce_only,
    )


# ── signed ownership ────────────────────────────────────────────────────
def test_a_naked_short_is_owned_not_foreign() -> None:
    """The hazard: under the long-only view a sold-to-open option nets
    negative and is dropped as "not ours" — leaving an unbounded-loss position
    that no safety path believes it owns."""
    trades = [_T(NIFTY_CE, OrderSide.SELL, 65)]
    assert net_owned(trades) == {}                       # the old, blind view
    assert net_owned_signed(trades) == {NIFTY_CE: Decimal("-65")}


def test_long_only_view_is_unchanged_for_cash_equity() -> None:
    """Every pre-036 caller keeps the exact behaviour it was written against."""
    trades = [
        _T("SWIGGY", OrderSide.BUY, 100),
        _T("SWIGGY", OrderSide.SELL, 40),
        _T("TBZ", OrderSide.BUY, 10),
        _T("TBZ", OrderSide.SELL, 10),
    ]
    assert net_owned(trades) == {"SWIGGY": Decimal("60")}


def test_an_unfilled_close_does_not_reduce_ownership() -> None:
    """Counting a not-yet-filled exit makes the bot abandon a position it
    still holds — the reason opening and closing use different state sets."""
    trades = [
        _T("SWIGGY", OrderSide.BUY, 100),
        _T("SWIGGY", OrderSide.SELL, 100, status=OrderStatus.PENDING,
           extra={"reduce_only": True}),
    ]
    assert net_owned_signed(trades) == {"SWIGGY": Decimal("100")}


def test_an_unfilled_sell_to_open_counts_immediately() -> None:
    """The window this exists to cover: between placing a naked short and its
    fill, the bot must already know the position is its own.

    The order carries ``reduce_only: False`` — stamped on EVERY order, because
    an absent key cannot distinguish "this opens exposure" from "this row
    predates the flag", and a sell-to-open would then fall back to the
    long-only rule that a SELL closes.
    """
    trades = [
        _T(NIFTY_CE, OrderSide.SELL, 65, status=OrderStatus.PENDING,
           extra={"reduce_only": False})
    ]
    assert net_owned_signed(trades) == {NIFTY_CE: Decimal("-65")}


def test_closing_a_short_uses_executed_states() -> None:
    """A BUY that closes a short must behave like an exit, not an entry —
    the direction, not the side, is what decides."""
    opened = _T(NIFTY_CE, OrderSide.SELL, 65)
    closing_pending = _T(NIFTY_CE, OrderSide.BUY, 65,
                         status=OrderStatus.PENDING, extra={"reduce_only": True})
    assert net_owned_signed([opened, closing_pending]) == {NIFTY_CE: Decimal("-65")}
    closing_filled = _T(NIFTY_CE, OrderSide.BUY, 65,
                        status=OrderStatus.FILLED, extra={"reduce_only": True})
    assert net_owned_signed([opened, closing_filled]) == {}


def test_an_unflagged_pending_sell_is_still_read_as_a_legacy_exit() -> None:
    """The deliberate limit of the fallback: with no flag there is no way to
    tell a sell-to-open from an exit, so the conservative reading wins and the
    position is only recognised once filled. Every order written from
    Decision 036 on carries the flag, so this affects historic rows only."""
    trades = [_T(NIFTY_CE, OrderSide.SELL, 65, status=OrderStatus.PENDING)]
    assert net_owned_signed(trades) == {}


def test_legacy_rows_without_the_flag_keep_the_long_only_rule() -> None:
    """Trades written before reduce_only was stamped must not be silently
    re-classified."""
    trades = [
        _T("SWIGGY", OrderSide.BUY, 100),
        _T("SWIGGY", OrderSide.SELL, 30, status=OrderStatus.FILLED),  # no flag
    ]
    assert net_owned_signed(trades) == {"SWIGGY": Decimal("70")}


# ── effective_holdings ──────────────────────────────────────────────────
def test_shorts_are_invisible_unless_asked_for() -> None:
    """Off by default because the live callers that would mis-read a negative
    quantity are on real money today."""
    positions = [_pos(NIFTY_CE, "short", 65)]
    owned = {NIFTY_CE: Decimal("-65")}
    assert effective_holdings(positions, owned) == {}
    assert effective_holdings(positions, owned, include_shorts=True) == {
        NIFTY_CE: Decimal("-65")
    }


def test_a_short_larger_than_ours_is_clipped_to_our_size() -> None:
    """Shared account: the user may be short the same contract."""
    positions = [_pos(NIFTY_CE, "short", 130)]
    got = effective_holdings(
        positions, {NIFTY_CE: Decimal("-65")}, include_shorts=True
    )
    assert got == {NIFTY_CE: Decimal("-65")}


# ── stop coverage for a short ───────────────────────────────────────────
def test_a_short_is_covered_by_a_buy_stop_not_a_sell() -> None:
    """A resting SELL cannot protect a short — it adds to it."""
    holdings = {NIFTY_CE: Decimal("-65")}
    wrong = check_stop_coverage(
        bucket_id="options-indian", holdings=holdings,
        open_orders=[_order(NIFTY_CE, "sell", 65)], sustain_ticks=1,
    )
    assert not wrong.ok and wrong.severity is Severity.HALT

    right = check_stop_coverage(
        bucket_id="options-indian", holdings=holdings,
        open_orders=[_order(NIFTY_CE, "buy", 65)], sustain_ticks=1,
    )
    assert right.ok


def test_a_long_is_still_covered_by_a_sell_stop() -> None:
    """The pre-036 behaviour, unchanged."""
    got = check_stop_coverage(
        bucket_id="swing-indian", holdings={"SWIGGY": Decimal("15")},
        open_orders=[_order("SWIGGY", "sell", 15)], sustain_ticks=1,
    )
    assert got.ok


def test_a_stop_too_small_to_cover_our_short_does_not_count() -> None:
    got = check_stop_coverage(
        bucket_id="options-indian", holdings={NIFTY_CE: Decimal("-130")},
        open_orders=[_order(NIFTY_CE, "buy", 65)], sustain_ticks=1,
    )
    assert not got.ok


# ── the expiry window: the blocker ──────────────────────────────────────
def test_a_stock_option_inside_the_window_halts() -> None:
    """Physically settled: an ITM contract carried past expiry DELIVERS SHARES
    at full contract value, which a Rs 5L bucket cannot fund."""
    got = check_expiry_window(
        bucket_id="options-indian",
        holdings={RELIANCE_CE: Decimal("500")},
        today=TODAY,
        cash_settled_underlyings=INDEXES,
    )
    assert not got.ok
    assert got.severity is Severity.HALT
    assert "PHYSICALLY SETTLED" in got.message
    assert "DELIVER SHARES" in got.message


def test_an_index_option_at_the_same_dte_is_fine() -> None:
    """Cash settled — the whole reason the two are distinguished."""
    got = check_expiry_window(
        bucket_id="options-indian",
        holdings={"NIFTY-20260902-24500-CE": Decimal("65")},
        today=TODAY,
        cash_settled_underlyings=INDEXES,
    )
    assert got.ok


def test_an_unknown_underlying_is_treated_as_physically_settled() -> None:
    """FAIL-SAFE, and deliberately so: a name missing from the index set costs
    an early square-off rather than a delivery obligation."""
    got = check_expiry_window(
        bucket_id="options-indian",
        holdings={"NIFTY-20260902-24500-CE": Decimal("65")},
        today=TODAY,
        cash_settled_underlyings=frozenset(),  # NIFTY not declared cash-settled
    )
    assert not got.ok
    assert "PHYSICALLY SETTLED" in got.message


def test_a_comfortable_expiry_passes() -> None:
    got = check_expiry_window(
        bucket_id="options-indian",
        holdings={NIFTY_CE: Decimal("65")},
        today=TODAY,
        cash_settled_underlyings=INDEXES,
    )
    assert got.ok


def test_an_expired_contract_still_on_the_books_halts_even_if_cash_settled() -> None:
    """Past expiry with a position still recorded is a reconciliation fault,
    and must not be silent just because nothing will be delivered."""
    got = check_expiry_window(
        bucket_id="futures-indian",
        holdings={"NIFTY-20260825-FUT": Decimal("65")},
        today=TODAY,
        cash_settled_underlyings=INDEXES,
    )
    assert not got.ok


def test_cash_equity_has_no_expiry_to_run_out_of() -> None:
    got = check_expiry_window(
        bucket_id="swing-indian",
        holdings={"SWIGGY": Decimal("100"), "NAM-INDIA": Decimal("50")},
        today=TODAY,
    )
    assert got.ok


def test_a_short_position_is_checked_for_expiry_too() -> None:
    """Signed quantities must not be skipped by a truthiness test."""
    got = check_expiry_window(
        bucket_id="options-indian",
        holdings={RELIANCE_CE: Decimal("-500")},
        today=TODAY,
        cash_settled_underlyings=INDEXES,
    )
    assert not got.ok


def test_expiry_detail_names_every_contract_at_risk() -> None:
    got = check_expiry_window(
        bucket_id="options-indian",
        holdings={RELIANCE_CE: Decimal("500"), NIFTY_CE: Decimal("65")},
        today=TODAY,
        cash_settled_underlyings=INDEXES,
    )
    assert set(got.detail["at_risk"]) == {RELIANCE_CE}


# ── margin utilisation ──────────────────────────────────────────────────
def test_margin_over_the_ceiling_halts() -> None:
    got = check_margin_utilisation(
        bucket_id="options-indian",
        used_margin_inr=Decimal("450000"),
        capital_inr=Decimal("500000"),
        max_utilisation=Decimal("0.80"),
    )
    assert not got.ok and got.severity is Severity.HALT


def test_margin_under_the_ceiling_passes() -> None:
    got = check_margin_utilisation(
        bucket_id="options-indian",
        used_margin_inr=Decimal("380000"),
        capital_inr=Decimal("500000"),
        max_utilisation=Decimal("0.80"),
    )
    assert got.ok


def test_absent_margin_figure_passes_rather_than_guessing() -> None:
    """On a shared account the reported margin covers the user's positions
    too, so an absent figure beats a wrong one — the notional ceiling still
    bounds the book."""
    got = check_margin_utilisation(
        bucket_id="options-indian",
        used_margin_inr=None,
        capital_inr=Decimal("500000"),
        max_utilisation=Decimal("0.80"),
    )
    assert got.ok


# ── the premium-multiple stop the bucket config must now allow ──────────
@pytest.mark.parametrize("pct", [Decimal("100"), Decimal("200"), Decimal("999")])
def test_bucket_accepts_a_premium_multiple_stop(pct: Decimal) -> None:
    """A short option's stop sits ABOVE entry, so 100 means "close when the
    premium doubles". The old lt=100 bound made that inexpressible."""
    from src.shared.bucket import BucketConfig

    cfg = BucketConfig(
        capital_inr=Decimal("500000"), broker="dhan",
        leverage_max=Decimal("1"), stop_loss_pct=pct,
    )
    assert cfg.stop_loss_pct == pct


def test_short_stop_sits_above_entry() -> None:
    """Sanity on the existing arithmetic, which Phase D relies on unchanged."""
    from src.safety.stop_protection import expected_trigger

    trigger = expected_trigger(Decimal("200"), "short", Decimal("100"))
    assert trigger == Decimal("400")  # premium doubles


def test_expiry_window_respects_a_custom_floor() -> None:
    got = check_expiry_window(
        bucket_id="options-indian",
        holdings={NIFTY_CE: Decimal("65")},
        today=TODAY,
        cash_settled_underlyings=INDEXES,
        cash_min_dte=10,  # 7 days out, floor 10
    )
    assert not got.ok
    assert got.detail["at_risk"][NIFTY_CE]["days_to_expiry"] == 7
    assert (TODAY + timedelta(days=7)).isoformat() == "2026-09-08"


# ── the account-level view must not call our own short foreign ──────────
def test_our_own_short_is_not_reported_as_the_users_position() -> None:
    """``PositionInfo.size`` is always positive with direction in ``side``,
    while signed ownership carries a short as a NEGATIVE net — so a raw
    comparison files every short the bot owns under the user's positions and
    pages about it every tick."""
    from src.safety.session_invariants import check_foreign_positions

    got = check_foreign_positions(
        account_ref="dhan",
        positions=[_pos(NIFTY_CE, "short", 65)],
        owned={NIFTY_CE: Decimal("-65")},
    )
    assert got.ok


def test_a_genuinely_foreign_position_is_still_reported() -> None:
    from src.safety.session_invariants import check_foreign_positions

    got = check_foreign_positions(
        account_ref="dhan",
        positions=[_pos("BANKNIFTY-20260929-57000-PE", "short", 30)],
        owned={NIFTY_CE: Decimal("-65")},
    )
    assert not got.ok
    assert got.severity is Severity.NOTICE


def test_a_user_position_larger_than_ours_is_still_flagged() -> None:
    """The 2026-07-22 case: the user holds the same scrip in size."""
    from src.safety.session_invariants import check_foreign_positions

    got = check_foreign_positions(
        account_ref="dhan",
        positions=[_pos("SWIGGY", "long", 100)],
        owned={"SWIGGY": Decimal("15")},
    )
    assert not got.ok


# ── a derivative bucket may hold a short; a cash bucket may not ─────────
def test_only_derivative_buckets_allow_shorts() -> None:
    """`Market.INDIAN` meant "cash equity" when the exit engine's short guard
    was written. commodity-indian is also INDIAN and holds shorts as a matter
    of course — 64 of its strategy's 125 backtested trades are sells — so
    gating on the market would drop every one from the exit engine and leave
    the position open and unmanaged."""
    from src.shared.bucket import load_buckets

    by_id = {b.id: b for b in load_buckets()}
    assert by_id["commodity-indian"].allows_shorts is True
    # Cash equity must keep refusing them: a demat account carries no short,
    # and Dhan reports a sale out of holdings as a negative day-position until
    # settlement (2026-08-18, PIIND).
    assert by_id["swing-indian"].allows_shorts is False
    assert by_id["intraday-indian"].allows_shorts is False
    # Crypto sub-accounts have always been able to.
    assert by_id["longterm-crypto"].allows_shorts is True
