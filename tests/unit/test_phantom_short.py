"""The phantom short — PIIND, 2026-08-18.

swing-indian sold its 15 PIIND at 12:16 IST. Selling stock out of HOLDINGS
shows up as a negative day-position in Dhan's /v2/positions until settlement
catches up, so the broker reported PIIND **short 15** for a few minutes.

The exit order was still PENDING, so `net_owned` had not yet decremented and
the symbol still looked owned. Three layers then acted on the artifact:

  1. the stop sweep planned a BUY stop ABOVE the market to "protect" it;
  2. the reconciler adopted it as a short Position row;
  3. that row is visible to `_run_exits`, which would compute the closing side
     as BUY and purchase 15 shares to close a position that does not exist.

Dhan rejected (1). Nothing would have stopped (3) — exits pass an engaged kill
switch by design (Decision 024).

The unifying rule: `net_owned` returns only positive (long) nets, so on a
shared account a SHORT can never be proven ours. Not ours ⇒ do not touch.
"""

from __future__ import annotations

from decimal import Decimal

from src.brokers.base import PositionInfo
from src.safety.stop_protection import plan_stop_protection

_PCTS = {"swing-indian": Decimal("20")}
_ATTR = {"PIIND": ("swing-indian", "mean_reversion_1h")}


def _pos(side: str, size: str = "15", entry: str = "2532.00") -> PositionInfo:
    return PositionInfo(
        symbol="PIIND", side=side, size=Decimal(size), entry_price=Decimal(entry)
    )


class TestSweepIgnoresPhantomShort:
    def test_no_stop_is_planned_for_a_short_on_a_shared_account(self) -> None:
        """The live failure: had Dhan accepted that BUY stop, triggering it
        would have BOUGHT 15 shares — the risk-reducing module opening a
        position."""
        plan = plan_stop_protection(
            positions=[_pos("short")],
            open_orders=[],
            stop_pct_by_bucket=_PCTS,
            attribution=_ATTR,
            # Still "owned": the exit was PENDING, and net_owned only
            # decrements on a FILLED sell.
            owned_quantities={"PIIND": Decimal("15")},
        )
        assert plan.place == []
        assert plan.cancel == []

    def test_a_long_on_the_same_account_is_still_protected(self) -> None:
        plan = plan_stop_protection(
            positions=[_pos("long", entry="2514.50")],
            open_orders=[],
            stop_pct_by_bucket=_PCTS,
            attribution=_ATTR,
            owned_quantities={"PIIND": Decimal("15")},
        )
        assert [(s.symbol, s.side) for s in plan.place] == [("PIIND", "sell")]

    def test_crypto_shorts_are_untouched(self) -> None:
        """Delta sub-accounts are exclusive (Decision 019) and shorting is a
        normal thing there — the guard must not reach them."""
        plan = plan_stop_protection(
            positions=[
                PositionInfo(symbol="BTCUSD", side="short",
                             size=Decimal("10"), entry_price=Decimal("50000"))
            ],
            open_orders=[],
            stop_pct_by_bucket={"longterm-crypto": Decimal("10")},
            attribution={"BTCUSD": ("longterm-crypto", "top5_volume")},
            owned_quantities=None,          # exclusive account
        )
        assert [(s.symbol, s.side) for s in plan.place] == [("BTCUSD", "buy")]


class TestExitEngineIgnoresPhantomShort:
    """The path with no backstop: exits bypass the kill switch."""

    def test_a_short_row_is_dropped_before_exits_are_selected(self) -> None:
        import inspect

        from src.shared.bucket_runner import BucketRunner

        src = inspect.getsource(BucketRunner._run_exits)
        assert "PositionSide.SHORT" in src, (
            "_run_exits must drop short rows — closing one computes side=BUY "
            "and would purchase shares to 'close' a position that isn't there"
        )
        assert "Market.INDIAN" in src, "the guard must be scoped to long-only equity"


class TestReconcilerDoesNotAdoptShorts:
    def test_adoption_and_cleanup_are_both_guarded(self) -> None:
        import inspect

        from src.order_manager.reconciler import Reconciler

        adopt = inspect.getsource(Reconciler._reconcile_positions)
        assert "short_position_not_adopted" in adopt, (
            "a short must never be imported as an orphan on a shared account"
        )
        assert "short_position_row_flattened" in adopt, (
            "an existing short row must be flattened — it is what feeds the "
            "exit engine the phantom position"
        )


class TestUnattributedGhostRows:
    """`bucket_id IS NULL` rows are immortal: everything that could close them
    scopes on `bucket_id.in_(ids)`, and NULL never matches IN.

    That looked harmless — exits, the stop sweep, attribution and the dashboard
    all scope by bucket. But `eod.py` reads positions with NO scoping, so a
    ghost is reported as CARRIED OVERNIGHT. On 2026-08-18 that would have
    claimed 15 PIIND and 238 PPLPHARMA the user had not held for days.
    """

    def test_eod_still_reads_positions_unscoped(self) -> None:
        """Pins WHY this cleanup has to exist. If eod.py ever starts scoping by
        bucket, this test fails and the reasoning above should be revisited."""
        import inspect

        from src.reporting import eod

        src = inspect.getsource(eod)
        assert "select(Position).where(Position.side != PositionSide.FLAT)" in src

    def test_cleanup_is_scoped_to_shared_accounts(self) -> None:
        """Crypto sub-accounts share a broker, so an unscoped sweep would let
        one Delta reconciler flatten a sibling sub-account's rows."""
        import inspect

        from src.order_manager.reconciler import Reconciler

        src = inspect.getsource(Reconciler._close_unattributed_positions)
        assert "if not self._shared_account:" in src
        assert "return" in src

    def test_a_sole_record_of_a_live_owned_position_is_kept(self) -> None:
        """Flattening the only trace of something the exchange still reports
        AND the bot still owns would be destroying state to tidy a report.

        The `unowned` arm is what covers T+1: a scrip sold today still shows in
        holdings tonight, so `absent` is False — but the ledger knows it is
        gone, and without that arm PIIND would have been reported as carried
        overnight on 2026-08-18."""
        import inspect

        from src.order_manager.reconciler import Reconciler

        src = inspect.getsource(Reconciler._close_unattributed_positions)
        assert "if not (duplicate or absent or unowned):" in src
        assert "continue" in src
        assert "bot_owned_quantities" in src, "ownership is the T+1 discriminator"
