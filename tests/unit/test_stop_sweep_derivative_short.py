"""A futures short is the bot's, and the sweep must treat it as such.

``tests/unit/test_phantom_short.py`` pins the opposite case and both must hold
at once. On CASH equity a short is an artifact (PIIND, 2026-08-18: selling out
of holdings shows as a negative day-position) and must be left alone. On a
DERIVATIVE bucket selling to open is an ordinary entry, ownership is signed,
and the same refusal is dangerous rather than safe.

The refusal sat BEFORE the attached-stop branch, so a skipped short never
reached ``plan.attached``. Two consequences, and the second is the serious one:

  1. ``check_stop_coverage`` reads a protected position as naked and HALTs.
  2. ``retire_legs`` sees a venue leg with no matching holding and CANCELS it —
     removing the only protection on a live short.

commodity-indian was short 1 NATGASMINI with an attached stop at 292.60 on the
night of 2026-09-04, so (2) was not hypothetical.
"""

from __future__ import annotations

from decimal import Decimal

from src.brokers.base import PositionInfo
from src.safety.stop_protection import plan_stop_protection

_SYM = "NATGASMINI-20260925-FUT"
_PCTS = {"commodity-indian": Decimal("4.5"), "swing-indian": Decimal("20")}
_ATTR = {_SYM: ("commodity-indian", "cci_gas_reversion_15m")}


def _short(size: str = "1", entry: str = "280.10") -> PositionInfo:
    return PositionInfo(
        symbol=_SYM, side="short", size=Decimal(size), entry_price=Decimal(entry)
    )


def _plan(**kw):
    base = dict(
        positions=[_short()],
        open_orders=[],
        stop_pct_by_bucket=_PCTS,
        attribution=_ATTR,
        # Signed: the bot's own short is negative.
        owned_quantities={_SYM: Decimal("-1")},
    )
    return plan_stop_protection(**{**base, **kw})


# ── the short is protected ──────────────────────────────────────────────


def test_a_futures_short_gets_a_stop() -> None:
    """A BUY stop ABOVE the entry is what protects a short. Refusing to plan
    one leaves an unbounded-loss position naked."""
    plan = _plan()
    assert len(plan.place) == 1
    order = plan.place[0]
    assert order.side == "buy"
    assert order.trigger > Decimal("280.10"), "a short's stop rests above it"
    assert order.bucket_id == "commodity-indian"


def test_the_stop_is_sized_to_the_bots_own_lots_not_a_negative() -> None:
    """A bare min(size, owned) yields -1, which falls through the `<= 0`
    guard and silently plans nothing at all."""
    plan = _plan(positions=[_short(size="3")], owned_quantities={_SYM: Decimal("-2")})
    assert len(plan.place) == 1
    assert plan.place[0].size == Decimal("2")


def test_a_cash_equity_short_is_still_refused() -> None:
    """PIIND. The artifact must never be acted on — same call, cash bucket."""
    plan = plan_stop_protection(
        positions=[
            PositionInfo(
                symbol="PIIND", side="short", size=Decimal("15"),
                entry_price=Decimal("2532.00"),
            )
        ],
        open_orders=[],
        stop_pct_by_bucket=_PCTS,
        attribution={"PIIND": ("swing-indian", "mean_reversion_1h")},
        owned_quantities={"PIIND": Decimal("15")},
    )
    assert plan.place == []
    assert plan.cancel == []


def test_an_unattributed_short_is_refused() -> None:
    """Attribution is late by design (the Position row is written by the
    5-minute reconciler). 'We do not know whose this is' is the artifact
    case, so the conservative answer wins."""
    plan = _plan(attribution={})
    assert plan.place == []


# ── the venue-attached leg survives ─────────────────────────────────────


def test_an_attached_leg_on_a_short_is_recognised() -> None:
    """Skipped before this branch, the leg never lands in plan.attached and
    the coverage invariant then HALTs a bucket that is in fact protected."""
    plan = _plan(attached_stops={_SYM: Decimal("292.60")})
    assert plan.attached == [_SYM]
    assert plan.place == [], "the venue already holds a stop; do not stack one"


def test_an_attached_leg_on_a_short_is_not_retired_as_an_orphan() -> None:
    """The dangerous one. Retiring cancels the only stop on a live short."""
    plan = _plan(attached_stops={_SYM: Decimal("292.60")})
    assert _SYM not in plan.retire_legs


def test_a_leg_with_no_position_is_still_retired() -> None:
    """The orphan pass must keep working — a leg for a position that is truly
    gone should not be left resting at the venue forever."""
    plan = _plan(
        positions=[],
        owned_quantities={},
        attached_stops={_SYM: Decimal("292.60")},
    )
    assert plan.retire_legs == [_SYM]
