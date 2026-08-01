"""Session invariants — pure logic, no I/O (mirrors test_stop_protection.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from src.brokers.base import OpenOrder, PositionInfo
from src.safety import session_invariants as si
from src.safety.session_invariants import (
    BucketWatch,
    Severity,
    check_bucket_liveness,
    check_foreign_positions,
    check_notional_ceiling,
    check_reject_rate,
    check_squareoff,
    check_stop_coverage,
    effective_holdings,
    enforce_session_invariants,
)
from src.shared.market_calendar import IST

INTRADAY = BucketWatch(
    bucket_id="intraday-indian",
    tick_interval_seconds=60,
    intraday=True,
    notional_budget_inr=Decimal("250000"),
)
SWING = BucketWatch(
    bucket_id="swing-indian",
    tick_interval_seconds=60,
    intraday=False,
    notional_budget_inr=Decimal("200000"),
)
CRYPTO = BucketWatch(bucket_id="longterm-crypto", tick_interval_seconds=900)


def _ist(day: str, hhmm: str) -> datetime:
    """A tz-aware IST datetime. 2026-07-28 is a Tuesday (trading day)."""
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=IST)


def _pos(symbol: str, size: str, entry: str = "100", side: str = "long") -> PositionInfo:
    return PositionInfo(
        symbol=symbol, side=side, size=Decimal(size), entry_price=Decimal(entry)
    )


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


# ---------------------------------------------------------------------------
# effective_holdings
# ---------------------------------------------------------------------------
def test_holdings_exclusive_account_takes_whole_position() -> None:
    assert effective_holdings([_pos("BTCUSD", "10")], None) == {"BTCUSD": Decimal("10")}


def test_holdings_shared_account_is_the_intersection() -> None:
    # Bot opened 5 of the 12 units on the account; the other 7 are the user's.
    holdings = effective_holdings([_pos("TCS", "12")], {"TCS": Decimal("5")})
    assert holdings == {"TCS": Decimal("5")}


def test_holdings_ignores_symbols_the_bot_does_not_own() -> None:
    assert effective_holdings([_pos("NIFTY24000CE", "50")], {"TCS": Decimal("5")}) == {}


def test_holdings_ledger_says_owned_but_exchange_is_flat() -> None:
    """Dhan's own MIS auto-square-off closes without writing our SELL row.

    Ownership alone would report a phantom holding forever and fail the
    square-off invariant every tick; the exchange is the tie-breaker.
    """
    assert effective_holdings([], {"TCS": Decimal("5")}) == {}


def test_holdings_skips_shorts_and_flats() -> None:
    positions = [_pos("A", "5", side="short"), _pos("B", "0")]
    assert effective_holdings(positions, None) == {}


# ---------------------------------------------------------------------------
# check_squareoff
# ---------------------------------------------------------------------------
def test_squareoff_ok_before_the_deadline() -> None:
    res = check_squareoff(
        bucket=INTRADAY,
        holdings={"TCS": Decimal("5")},
        now=_ist("2026-07-28", "15:14"),
        grace_minutes=5,
    )
    assert res.ok


def test_squareoff_ok_within_grace() -> None:
    res = check_squareoff(
        bucket=INTRADAY,
        holdings={"TCS": Decimal("5")},
        now=_ist("2026-07-28", "15:19"),
        grace_minutes=5,
    )
    assert res.ok


def test_squareoff_violated_after_grace_and_halts() -> None:
    res = check_squareoff(
        bucket=INTRADAY,
        holdings={"TCS": Decimal("5")},
        now=_ist("2026-07-28", "15:21"),
        grace_minutes=5,
    )
    assert not res.ok
    assert res.severity is Severity.HALT
    assert "TCS" in res.message


def test_squareoff_still_fires_after_the_close() -> None:
    """An un-squared MIS position at 16:00 is worse, not stale."""
    res = check_squareoff(
        bucket=INTRADAY,
        holdings={"TCS": Decimal("5")},
        now=_ist("2026-07-28", "16:00"),
        grace_minutes=5,
    )
    assert not res.ok


def test_squareoff_ok_when_flat() -> None:
    res = check_squareoff(
        bucket=INTRADAY, holdings={}, now=_ist("2026-07-28", "15:25"), grace_minutes=5
    )
    assert res.ok


def test_squareoff_never_applies_to_a_carrying_bucket() -> None:
    """swing-indian holds MTF for days — 15:15 means nothing to it."""
    res = check_squareoff(
        bucket=SWING,
        holdings={"TCS": Decimal("5")},
        now=_ist("2026-07-28", "16:00"),
        grace_minutes=5,
    )
    assert res.ok


def test_squareoff_ignores_non_trading_days() -> None:
    # 2026-07-26 is a Sunday.
    res = check_squareoff(
        bucket=INTRADAY,
        holdings={"TCS": Decimal("5")},
        now=_ist("2026-07-26", "16:00"),
        grace_minutes=5,
    )
    assert res.ok


# ---------------------------------------------------------------------------
# check_stop_coverage
# ---------------------------------------------------------------------------
def test_stop_coverage_ok_when_every_holding_has_a_resting_stop() -> None:
    res = check_stop_coverage(
        bucket_id="swing-indian",
        holdings={"TCS": Decimal("5")},
        open_orders=[_stop("TCS")],
        sustain_ticks=2,
    )
    assert res.ok


def test_stop_coverage_flags_a_naked_position() -> None:
    res = check_stop_coverage(
        bucket_id="swing-indian",
        holdings={"TCS": Decimal("5"), "INFY": Decimal("3")},
        open_orders=[_stop("TCS")],
        sustain_ticks=2,
    )
    assert not res.ok
    assert res.severity is Severity.HALT
    assert res.detail["uncovered"] == ["INFY"]
    assert res.sustain_ticks == 2


def test_stop_coverage_ignores_a_non_stop_order() -> None:
    entry = OpenOrder(
        exchange_order_id="9",
        client_order_id=None,
        symbol="TCS",
        side="buy",
        size=Decimal("5"),
        unfilled_size=Decimal("5"),
        order_type="limit",
        limit_price=Decimal("100"),
        status="open",
        stop_price=None,
        reduce_only=False,
    )
    res = check_stop_coverage(
        bucket_id="swing-indian",
        holdings={"TCS": Decimal("5")},
        open_orders=[entry],
        sustain_ticks=2,
    )
    assert not res.ok


def test_stop_coverage_ignores_a_fully_filled_stop() -> None:
    res = check_stop_coverage(
        bucket_id="swing-indian",
        holdings={"TCS": Decimal("5")},
        open_orders=[_stop("TCS", unfilled="0")],
        sustain_ticks=2,
    )
    assert not res.ok


# ---------------------------------------------------------------------------
# check_notional_ceiling
# ---------------------------------------------------------------------------
def test_notional_ok_at_full_budget() -> None:
    # 5 slots × Rs 50k notional = the bucket's whole 50k × 5x budget.
    holdings = {f"S{i}": Decimal("500") for i in range(5)}
    res = check_notional_ceiling(
        bucket=INTRADAY,
        holdings=holdings,
        entry_prices={f"S{i}": Decimal("100") for i in range(5)},
        tolerance=Decimal("1.10"),
    )
    assert res.ok


def test_notional_breached_halts() -> None:
    res = check_notional_ceiling(
        bucket=INTRADAY,
        holdings={"TCS": Decimal("5000")},  # Rs 500k on a Rs 250k budget
        entry_prices={"TCS": Decimal("100")},
        tolerance=Decimal("1.10"),
    )
    assert not res.ok
    assert res.severity is Severity.HALT


def test_notional_skipped_when_no_inr_budget() -> None:
    """Crypto is contract-denominated and USD-priced — never ceiling-checked."""
    res = check_notional_ceiling(
        bucket=CRYPTO,
        holdings={"BTCUSD": Decimal("10000")},
        entry_prices={"BTCUSD": Decimal("50000")},
        tolerance=Decimal("1.10"),
    )
    assert res.ok


def test_notional_missing_price_does_not_crash() -> None:
    res = check_notional_ceiling(
        bucket=SWING,
        holdings={"TCS": Decimal("5")},
        entry_prices={},
        tolerance=Decimal("1.10"),
    )
    assert res.ok


# ---------------------------------------------------------------------------
# check_reject_rate
# ---------------------------------------------------------------------------
def test_reject_rate_ok_below_threshold() -> None:
    res = check_reject_rate(
        bucket_id="swing-indian", rejected=2, threshold=3, window_minutes=15
    )
    assert res.ok


def test_reject_rate_halts_at_threshold() -> None:
    res = check_reject_rate(
        bucket_id="swing-indian", rejected=3, threshold=3, window_minutes=15
    )
    assert not res.ok
    assert res.severity is Severity.HALT


# ---------------------------------------------------------------------------
# check_bucket_liveness
# ---------------------------------------------------------------------------
def test_liveness_ok_within_the_stale_window() -> None:
    now = _ist("2026-07-28", "11:00")
    res = check_bucket_liveness(
        bucket=INTRADAY,
        beat_at=now - timedelta(seconds=120),  # 2 cadences
        now=now,
        stale_multiple=3.0,
    )
    assert res.ok


def test_liveness_flags_a_stalled_bucket_as_notice_only() -> None:
    """NOTICE, never HALT: a bucket that isn't running already isn't entering."""
    now = _ist("2026-07-28", "11:00")
    res = check_bucket_liveness(
        bucket=INTRADAY,
        beat_at=now - timedelta(seconds=600),
        now=now,
        stale_multiple=3.0,
    )
    assert not res.ok
    assert res.severity is Severity.NOTICE


def test_liveness_never_beat_is_stale() -> None:
    res = check_bucket_liveness(
        bucket=INTRADAY,
        beat_at=None,
        now=_ist("2026-07-28", "11:00"),
        stale_multiple=3.0,
    )
    assert not res.ok


def test_liveness_scales_with_the_buckets_own_cadence() -> None:
    """A 900s crypto bucket is not stale at 600s; a 60s equity bucket is."""
    now = _ist("2026-07-28", "11:00")
    beat_at = now - timedelta(seconds=600)
    assert check_bucket_liveness(
        bucket=CRYPTO, beat_at=beat_at, now=now, stale_multiple=3.0
    ).ok
    assert not check_bucket_liveness(
        bucket=INTRADAY, beat_at=beat_at, now=now, stale_multiple=3.0
    ).ok


# ---------------------------------------------------------------------------
# check_foreign_positions
# ---------------------------------------------------------------------------
def test_foreign_positions_reported_but_never_halted() -> None:
    """The 2026-07-22 near-miss: the user's NIFTY options on the bot's account."""
    res = check_foreign_positions(
        account_ref="dhan",
        positions=[_pos("NIFTY24000CE", "50"), _pos("TCS", "5")],
        owned={"TCS": Decimal("5")},
    )
    assert not res.ok
    assert res.severity is Severity.NOTICE
    assert res.detail["foreign"] == ["NIFTY24000CE"]


def test_foreign_positions_flags_a_partially_owned_symbol() -> None:
    res = check_foreign_positions(
        account_ref="dhan",
        positions=[_pos("TCS", "12")],
        owned={"TCS": Decimal("5")},
    )
    assert not res.ok
    assert res.detail["foreign"] == ["TCS"]


def test_foreign_positions_silent_on_an_exclusive_account() -> None:
    res = check_foreign_positions(
        account_ref="default", positions=[_pos("BTCUSD", "10")], owned=None
    )
    assert res.ok


# ---------------------------------------------------------------------------
# enforce_session_invariants — the acting path
# ---------------------------------------------------------------------------
class _Recorder:
    """Stands in for kill_switch + alerts so nothing touches a DB or Telegram."""

    def __init__(self) -> None:
        self.engaged: list[str] = []
        self.alerts: list[str] = []

    def install(self, monkeypatch, engaged_already: bool = False) -> None:
        monkeypatch.setattr(
            si.kill_switch,
            "engage",
            lambda reason, *, strategy_id=None, engaged_by="", clock=None: (
                self.engaged.append(strategy_id)
            ),
        )
        monkeypatch.setattr(
            si.kill_switch, "is_engaged", lambda bucket_id=None: engaged_already
        )
        monkeypatch.setattr(
            si, "send_alert_dedup", lambda key, msg: self.alerts.append(key)
        )
        monkeypatch.setattr(si, "note_alert_recovery", lambda key, msg: None)
        si.reset_streaks()


def _violation(name: str, severity: Severity, sustain: int = 1) -> si.InvariantResult:
    return si.InvariantResult(
        name, "intraday-indian", ok=False, severity=severity, message="x",
        sustain_ticks=sustain,
    )


def test_enforce_notice_alerts_but_never_halts(monkeypatch) -> None:
    rec = _Recorder()
    rec.install(monkeypatch)
    halted = enforce_session_invariants([_violation("bucket_liveness", Severity.NOTICE)])
    assert halted == []
    assert rec.engaged == []
    assert rec.alerts  # still paged


def test_enforce_halts_immediately_at_sustain_one(monkeypatch) -> None:
    rec = _Recorder()
    rec.install(monkeypatch)
    halted = enforce_session_invariants([_violation("squareoff", Severity.HALT)])
    assert len(halted) == 1
    assert rec.engaged == ["intraday-indian"]


def test_enforce_waits_for_the_sustain_streak(monkeypatch) -> None:
    """One uncovered reading can race a just-placed stop; two cannot."""
    rec = _Recorder()
    rec.install(monkeypatch)
    res = _violation("stop_coverage", Severity.HALT, sustain=2)
    assert enforce_session_invariants([res]) == []
    assert rec.engaged == []
    assert len(enforce_session_invariants([res])) == 1
    assert rec.engaged == ["intraday-indian"]


def test_enforce_streak_resets_when_the_check_passes(monkeypatch) -> None:
    rec = _Recorder()
    rec.install(monkeypatch)
    bad = _violation("stop_coverage", Severity.HALT, sustain=2)
    good = si.InvariantResult("stop_coverage", "intraday-indian", ok=True)
    enforce_session_invariants([bad])
    enforce_session_invariants([good])  # transient — streak must clear
    assert enforce_session_invariants([bad]) == []
    assert rec.engaged == []


def test_enforce_does_not_re_engage_an_already_killed_bucket(monkeypatch) -> None:
    rec = _Recorder()
    rec.install(monkeypatch, engaged_already=True)
    assert enforce_session_invariants([_violation("squareoff", Severity.HALT)]) == []
    assert rec.engaged == []


def test_a_steady_state_violation_pages_once_not_every_tick(monkeypatch) -> None:
    """The 2026-07-28 bug: foreign_positions paged ~72x a day.

    send_alert_dedup's window re-arms hourly, which is right for a recurring
    transient and wrong for a condition that is simply always true. Noise on
    its own — and it buries the alerts this system exists to deliver.
    """
    rec = _Recorder()
    rec.install(monkeypatch)
    res = si.InvariantResult(
        "foreign_positions", "dhan", ok=False, severity=Severity.NOTICE,
        message="not the bot's", detail={"foreign": ["NIFTY24000CE"]},
    )
    for _ in range(50):
        enforce_session_invariants([res])
    assert len(rec.alerts) == 1


def test_a_changed_violation_pages_again(monkeypatch) -> None:
    """Silence is for an UNCHANGED condition — new content is news."""
    rec = _Recorder()
    rec.install(monkeypatch)
    first = si.InvariantResult(
        "foreign_positions", "dhan", ok=False, severity=Severity.NOTICE,
        message="x", detail={"foreign": ["A"]},
    )
    second = si.InvariantResult(
        "foreign_positions", "dhan", ok=False, severity=Severity.NOTICE,
        message="x", detail={"foreign": ["A", "B"]},
    )
    enforce_session_invariants([first])
    enforce_session_invariants([first])
    enforce_session_invariants([second])
    assert len(rec.alerts) == 2


def test_clearing_then_recurring_pages_again(monkeypatch) -> None:
    """Suppression must not outlive the condition that caused it."""
    rec = _Recorder()
    rec.install(monkeypatch)
    bad = si.InvariantResult(
        "stop_coverage", "swing-indian", ok=False, severity=Severity.NOTICE,
        message="x", detail={"uncovered": ["TCS"]},
    )
    good = si.InvariantResult("stop_coverage", "swing-indian", ok=True)
    enforce_session_invariants([bad])
    enforce_session_invariants([good])
    enforce_session_invariants([bad])
    assert len(rec.alerts) == 2


def test_observe_only_pages_but_never_halts(monkeypatch) -> None:
    """Default mode: a brand-new invariant cannot halt a live bucket."""
    rec = _Recorder()
    rec.install(monkeypatch)
    halted = enforce_session_invariants(
        [_violation("squareoff", Severity.HALT)], enforcing=False
    )
    assert halted == []
    assert rec.engaged == []
    assert rec.alerts  # still paged


def test_observe_only_message_says_it_would_have_halted(monkeypatch) -> None:
    rec = _Recorder()
    sent: list[str] = []
    rec.install(monkeypatch)
    monkeypatch.setattr(si, "send_alert_dedup", lambda key, msg: sent.append(msg))
    enforce_session_invariants(
        [_violation("squareoff", Severity.HALT)], enforcing=False
    )
    assert sent[0].startswith("[OBSERVE-ONLY, would have HALTED]")


def test_observe_only_leaves_a_notice_message_unprefixed(monkeypatch) -> None:
    rec = _Recorder()
    sent: list[str] = []
    rec.install(monkeypatch)
    monkeypatch.setattr(si, "send_alert_dedup", lambda key, msg: sent.append(msg))
    enforce_session_invariants(
        [_violation("bucket_liveness", Severity.NOTICE)], enforcing=False
    )
    assert not sent[0].startswith("[OBSERVE-ONLY")


def test_observe_only_still_tracks_the_sustain_streak(monkeypatch) -> None:
    """Streaks must keep counting, so flipping to enforcing needs no warm-up."""
    rec = _Recorder()
    sent: list[str] = []
    rec.install(monkeypatch)
    monkeypatch.setattr(si, "send_alert_dedup", lambda key, msg: sent.append(msg))
    res = _violation("stop_coverage", Severity.HALT, sustain=2)
    enforce_session_invariants([res], enforcing=False)
    enforce_session_invariants([res], enforcing=False)
    assert not sent[0].startswith("[OBSERVE-ONLY")  # tick 1: below the streak
    assert sent[1].startswith("[OBSERVE-ONLY")  # tick 2: would have halted


def test_a_volatile_detail_does_not_defeat_suppression(monkeypatch) -> None:
    """bucket_liveness carries an age that grows every tick.

    Keyed on detail alone, a stalled bucket would page forever — the exact
    spam this suppression exists to stop.
    """
    rec = _Recorder()
    rec.install(monkeypatch)
    now = _ist("2026-07-28", "11:00")
    for extra in range(0, 500, 60):
        res = check_bucket_liveness(
            bucket=INTRADAY,
            beat_at=now - timedelta(seconds=600 + extra),
            now=now,
            stale_multiple=3.0,
        )
        enforce_session_invariants([res])
    assert len(rec.alerts) == 1


def test_escalation_to_would_halt_still_pages(monkeypatch) -> None:
    """Suppression must not swallow the moment a warning becomes a halt."""
    rec = _Recorder()
    sent: list[str] = []
    rec.install(monkeypatch)
    monkeypatch.setattr(si, "send_alert_dedup", lambda key, msg: sent.append(msg))
    res = _violation("stop_coverage", Severity.HALT, sustain=2)
    enforce_session_invariants([res], enforcing=False)
    enforce_session_invariants([res], enforcing=False)
    assert len(sent) == 2
    assert not sent[0].startswith("[OBSERVE-ONLY")
    assert sent[1].startswith("[OBSERVE-ONLY")
