"""Decision 038 — a missing stop HALTS during the session and PAGES outside it.

`stop_coverage` asserts every held position carries a resting reduce-only stop.
Dhan expires DAY orders at the close, so once the venue shuts no stop CAN rest,
and the check was firing every night against a condition nothing could resolve.
Halting there bought nothing — per Decision 024 exits, the sweep and the
breakers all keep running while killed, and the kill switch does not create a
stop — while costing a bucket that was then dead at the next open.

On 2026-09-04 it halted commodity-indian at 00:49 IST over NATGASMINI and
swing-indian at 01:34 IST over KEI. Both stayed down six days.

This is a severity change, NOT an exemption: outside the session the violation
still fires, still pages and is still audited. Inside the session a missing
stop means the sweep genuinely failed, and that still halts.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from src.brokers.base import OpenOrder
from src.safety.session_invariants import Severity, check_stop_coverage
from src.shared.market_calendar import IST, NseSession, nse_session


def _stop(symbol: str, unfilled: str = "10") -> OpenOrder:
    return OpenOrder(
        exchange_order_id="1",
        client_order_id=None,
        symbol=symbol,
        side="sell",
        size=Decimal(unfilled),
        unfilled_size=Decimal(unfilled),
        order_type="market",
        limit_price=None,
        status="open",
        stop_price=Decimal("90"),
        reduce_only=True,
    )


def _naked(session_open: bool):
    return check_stop_coverage(
        bucket_id="swing-indian",
        holdings={"KEI": Decimal("7")},
        open_orders=[],
        sustain_ticks=2,
        session_open=session_open,
    )


# ── the severity split ──────────────────────────────────────────────────


def test_a_naked_position_still_halts_during_the_session() -> None:
    """The case the invariant exists for: the sweep ran and left nothing."""
    res = _naked(session_open=True)
    assert not res.ok
    assert res.severity is Severity.HALT
    assert res.detail["uncovered"] == ["KEI"]


def test_the_same_position_only_notices_once_the_venue_shuts() -> None:
    """KEI, 2026-09-05 01:34 IST. Still a violation, still paged — but the
    kill switch is never reached, because `would_halt` requires HALT."""
    res = _naked(session_open=False)
    assert not res.ok
    assert res.severity is Severity.NOTICE
    assert res.detail["uncovered"] == ["KEI"]


def test_the_overnight_message_says_why_it_is_not_halting() -> None:
    """A notice that reads like the halt would train the reader to ignore it."""
    assert "Not halting" in _naked(session_open=False).message
    assert "gap" in _naked(session_open=False).message


def test_a_covered_position_is_ok_either_way() -> None:
    for session_open in (True, False):
        res = check_stop_coverage(
            bucket_id="swing-indian",
            holdings={"KEI": Decimal("7")},
            open_orders=[_stop("KEI")],
            sustain_ticks=2,
            session_open=session_open,
        )
        assert res.ok


def test_the_gate_defaults_to_halting() -> None:
    """Every caller that has not been taught about sessions keeps pre-038
    behaviour — the safe direction for a defaulted safety flag."""
    res = check_stop_coverage(
        bucket_id="swing-indian",
        holdings={"KEI": Decimal("7")},
        open_orders=[],
        sustain_ticks=2,
    )
    assert res.severity is Severity.HALT


# ── the reason the gate is per-bucket and not per-account ───────────────


def test_mcx_is_still_open_when_nse_has_closed() -> None:
    """16:00 IST on a Friday. commodity-indian and swing-indian share one Dhan
    account; an account-level NSE answer would downgrade a genuine naked MCX
    short for the eight hours MCX is still trading — the window the NATGASMINI
    short was actually live in."""
    at_16 = datetime(2026, 9, 4, 16, 0, tzinfo=IST)
    assert nse_session(at_16, exchange="NSE") is NseSession.CLOSED
    assert nse_session(at_16, exchange="MCX") is not NseSession.CLOSED


def test_both_venues_are_shut_at_the_hour_the_halts_fired() -> None:
    """01:15 IST — the alarms that started this. Neither venue can rest a stop,
    so neither bucket should have been halted."""
    at_0115 = datetime(2026, 9, 5, 1, 15, tzinfo=IST)
    for exchange in ("NSE", "MCX"):
        assert nse_session(at_0115, exchange=exchange) is NseSession.CLOSED
