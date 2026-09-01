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
    check_scan_coverage,
    check_signal_delivery,
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
        self.audited: list[tuple[str, bool]] = []

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
        # audit_violation opens a real session. Stubbed for the same reason as
        # the alert sender: a unit test must not reach the live database.
        monkeypatch.setattr(
            si,
            "audit_violation",
            lambda res, *, streak, would_halt, enforcing: self.audited.append(
                (res.name, would_halt)
            ),
        )
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


# ---------------------------------------------------------------------------
# audit_violation — the durable record (Decision 033)
# ---------------------------------------------------------------------------
# Before this existed an invariant left no trace in Postgres unless it escalated
# to HALT (which writes KILL_SWITCH_FLIPPED from kill_switch.engage). The EOD
# journal is built entirely from audit rows, so every violation that cleared
# before halting anything was reported as "nothing tripped".
def test_violation_is_recorded_in_the_audit_log(monkeypatch) -> None:
    rec = _Recorder()
    rec.install(monkeypatch)
    enforce_session_invariants([_violation("stop_coverage", Severity.NOTICE)])
    assert rec.audited == [("stop_coverage", False)]


def test_a_steady_violation_records_once_not_every_tick(monkeypatch) -> None:
    """The audit log must not reproduce the Telegram flood of 2026-07-28.

    foreign_positions is permanently violated whenever the user holds anything
    on the shared Dhan account. At one row per 60s tick that is ~500 rows a
    session about positions the bot is correctly ignoring.
    """
    rec = _Recorder()
    rec.install(monkeypatch)
    res = si.InvariantResult(
        "foreign_positions", "dhan", ok=False, severity=Severity.NOTICE,
        message="x", detail={"foreign": ["A"]},
    )
    for _ in range(10):
        enforce_session_invariants([res])
    assert len(rec.audited) == 1


def test_a_changed_violation_records_again(monkeypatch) -> None:
    """New content is new news — for the record as much as for the page."""
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
    assert len(rec.audited) == 2


def test_escalation_to_would_halt_is_recorded(monkeypatch) -> None:
    """A warning becoming a halt must reach the journal, not just Telegram."""
    rec = _Recorder()
    rec.install(monkeypatch)
    res = _violation("squareoff", Severity.HALT, sustain=2)
    enforce_session_invariants([res], enforcing=False)
    enforce_session_invariants([res], enforcing=False)
    assert [halt for _, halt in rec.audited] == [False, True]


def test_a_passing_check_records_nothing(monkeypatch) -> None:
    rec = _Recorder()
    rec.install(monkeypatch)
    enforce_session_invariants([si.InvariantResult("squareoff", "dhan", ok=True)])
    assert rec.audited == []


def test_audit_write_failure_never_breaks_the_safety_loop(monkeypatch) -> None:
    """A monitor that killed the checks it records would be worse than none."""
    rec = _Recorder()
    rec.install(monkeypatch)

    def _explode():
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(si, "session_scope", _explode)
    monkeypatch.setattr(si, "audit_violation", si.audit_violation)  # real one

    halted = enforce_session_invariants([_violation("squareoff", Severity.HALT)])
    assert halted  # the halt still happened
    assert rec.engaged == ["intraday-indian"]


# ── scan_coverage: the 2026-08-04/05 blind-scanner invariant ─────────────
def _coverage(**kw) -> si.ScanCoverage:
    base = {
        "bucket_id": "swing-indian",
        "scanner_id": "swing-indian",
        "configured": 94,
        "attempted": 94,
        "evaluated": 94,
        "unevaluable": 0,
        "ts": datetime(2026, 8, 6, 14, 46, tzinfo=IST),
    }
    base.update(kw)
    return si.ScanCoverage(**base)  # type: ignore[arg-type]


def _scan(coverage, ratio: float = 0.9):
    return check_scan_coverage(
        coverage=coverage, bucket_id="swing-indian", unevaluable_ratio=ratio
    )


def test_scan_coverage_passes_on_a_healthy_scan() -> None:
    assert _scan(_coverage()).ok is True


def test_scan_coverage_reports_when_every_fetch_failed() -> None:
    """The outage itself: 94 symbols attempted, 0 evaluated, for two days."""
    res = _scan(_coverage(evaluated=0))
    assert res.ok is False
    assert "SCANNER BLIND" in res.message
    assert "not a quiet market" in res.message.lower()


def test_scan_coverage_reports_when_the_universe_collapsed() -> None:
    """A bad scrip master empties the F&O filter before any fetch is tried."""
    res = _scan(_coverage(attempted=0, evaluated=0))
    assert res.ok is False
    assert "universe collapsed" in res.message


def test_a_blind_scanner_is_never_halted() -> None:
    """It evaluates nothing, so it enters nothing — halting prevents nothing
    already prevented, and the kill switch only clears by hand, so a six-second
    token blip in the 15:15 bin would skip the NEXT morning too."""
    for cov in (_coverage(evaluated=0), _coverage(attempted=0, evaluated=0)):
        res = _scan(cov)
        assert res.ok is False
        assert res.severity is Severity.NOTICE
        assert "halt" not in res.message.lower()


def test_scan_coverage_only_notices_degraded_data() -> None:
    """Nearly-all-unusable has honest causes (a stub bin after a restart), so
    it must page without halting — a wrongful halt costs a trading day."""
    res = _scan(_coverage(evaluated=94, unevaluable=93))
    assert res.ok is False
    assert res.severity is Severity.NOTICE
    assert "DEGRADED" in res.message


def test_scan_coverage_is_silent_when_no_scan_was_recorded() -> None:
    """Silence is bucket_liveness's job. Double-reporting one fault as two
    invariants would make the Telegram digest lie about how much is wrong."""
    assert _scan(None).ok is True


def test_scan_coverage_ignores_an_empty_configured_universe() -> None:
    """A scanner set with no symbols configured is disabled, not blind."""
    assert _scan(_coverage(configured=0, attempted=0, evaluated=0)).ok is True


# ── coverage_from_payload: "absent" must never mean "looked at nothing" ──
def _from_payload(payload):
    return si.coverage_from_payload(
        bucket_id="swing-indian",
        scanner_id="swing-indian",
        payload=payload,
        ts=datetime(2026, 8, 6, 14, 46, tzinfo=IST),
    )


def test_payload_without_counts_cannot_halt_a_bucket() -> None:
    """The crypto run_scan engine and every pre-2026-08-07 row record no
    funnel. Defaulting those to 0 would halt buckets that are working."""
    assert _from_payload({"bucket_id": "x", "universe": []}) is None
    assert _from_payload(None) is None
    assert _scan(_from_payload({"universe": []})).ok is True


# ── signal_delivery: the 2026-08-07 BLUESTARCO miss ──────────────────────
def _delivery(lost):
    return check_signal_delivery(
        bucket_id="swing-indian", lost=lost, window_minutes=90
    )


def test_signal_delivery_passes_when_nothing_was_lost() -> None:
    assert _delivery([]).ok is True


def test_signal_delivery_reports_a_signal_lost_to_a_data_failure() -> None:
    """A healthy scan found BLUESTARCO; the quote endpoint 401'd; the sizer had
    no price to turn Rs 40,000 into shares. scan_coverage cannot see this —
    the scan was fine. This is the check that can."""
    res = _delivery(["BLUESTARCO"])
    assert res.ok is False
    assert "SIGNAL LOST TO DATA" in res.message
    assert "BLUESTARCO" in res.message
    assert res.detail["lost"] == ["BLUESTARCO"]


def test_signal_delivery_never_halts() -> None:
    """The trade is already lost; halting only stops the NEXT one."""
    assert _delivery(["BLUESTARCO"]).severity is Severity.NOTICE


def test_signal_delivery_dedups_repeated_misses_of_one_symbol() -> None:
    """38 retries of the same signal is one problem, not 38."""
    res = _delivery(["BLUESTARCO"] * 38)
    assert res.detail["lost"] == ["BLUESTARCO"]
    assert res.alert_signature == "BLUESTARCO"


def test_signal_delivery_pages_again_when_a_new_symbol_is_lost() -> None:
    first = _delivery(["BLUESTARCO"])
    second = _delivery(["BLUESTARCO", "SUZLON"])
    assert first.alert_signature != second.alert_signature


def test_malformed_counts_are_unknown_not_zero() -> None:
    assert _from_payload({"attempted": "lots"}) is None


def test_payload_with_counts_is_read_through() -> None:
    cov = _from_payload(
        {"configured": 94, "attempted": 94, "evaluated": 0, "unevaluable": 0}
    )
    assert cov is not None
    assert cov.evaluated == 0
    assert _scan(cov).ok is False


def test_no_scan_coverage_outcome_can_touch_the_kill_switch() -> None:
    for cov in (
        _coverage(evaluated=0),
        _coverage(attempted=0, evaluated=0),
        _coverage(evaluated=94, unevaluable=93),
    ):
        assert _scan(cov).severity is Severity.NOTICE


def test_the_two_no_price_reasons_are_distinct() -> None:
    """The whole check rests on telling a failed fetch from a declined trade.

    If these two ever collapsed to one string, signal_delivery would either
    page on every ordinary skip or on none of them.
    """
    from src.shared.allocator import sizer

    assert sizer.PRICE_FETCH_FAILED_REASON != sizer.NO_MARK_PRICE_REASON


def test_signal_delivery_matches_the_sizer_constant_exactly() -> None:
    """Matched by constant, not substring, so a reword cannot silently disarm
    the alarm — and an alarm's job is to work when other things have broken."""
    import inspect

    from src.shared.allocator import sizer

    src = inspect.getsource(si.signals_lost_to_data)
    assert "PRICE_FETCH_FAILED_REASON" in src
    assert sizer.NO_MARK_PRICE_REASON not in src


# ---------------------------------------------------------------------------
# kill_switch_dwell — Phase 11a
#
# The gap this closes: a halt is invisible to BOTH other perception checks.
# bucket_liveness reads the bucket heartbeat, which the halted path still
# beats; scan_coverage sees no SCANNER_RUN and defers to liveness by design.
# In August 2026 a stop_coverage halt on PIIND held swing-indian down from
# 08-12 13:18 to 08-18 15:05 — four sessions — and paged nobody.
# ---------------------------------------------------------------------------
NOW = _ist("2026-07-28", "11:00")


def test_dwell_ok_when_switch_is_clear():
    r = si.check_kill_switch_dwell(
        bucket_id="swing-indian", engaged_at=None, now=NOW, max_dwell_minutes=120
    )
    assert r.ok


def test_dwell_ok_inside_threshold():
    """A halt taken and cleared within a session must stay quiet."""
    r = si.check_kill_switch_dwell(
        bucket_id="swing-indian",
        engaged_at=NOW - timedelta(minutes=119),
        now=NOW,
        max_dwell_minutes=120,
    )
    assert r.ok


def test_dwell_notices_past_threshold():
    r = si.check_kill_switch_dwell(
        bucket_id="swing-indian",
        engaged_at=NOW - timedelta(minutes=121),
        now=NOW,
        max_dwell_minutes=120,
    )
    assert not r.ok
    assert r.severity is Severity.NOTICE  # never HALT — it is already halted
    assert "swing-indian" in r.message
    assert r.detail["dwell_minutes"] == 121


def test_dwell_reports_the_august_outage():
    """The real one: 08-12 13:18 -> 08-18 15:05, and nothing said so."""
    engaged = _ist("2026-08-12", "13:18")
    r = si.check_kill_switch_dwell(
        bucket_id="swing-indian",
        engaged_at=engaged,
        now=_ist("2026-08-17", "09:16"),  # the Monday it was blind
        max_dwell_minutes=120,
    )
    assert not r.ok
    assert "116.0h" in r.message  # 6,958 minutes of blindness
    assert r.detail["dwell_minutes"] == 6958
    assert "2026-08-12 13:18" in r.message


def test_dwell_pages_once_not_every_tick():
    """dwell_minutes grows every tick; the signature must not."""
    a = si.check_kill_switch_dwell(
        bucket_id="swing-indian",
        engaged_at=NOW - timedelta(minutes=200),
        now=NOW,
        max_dwell_minutes=120,
    )
    b = si.check_kill_switch_dwell(
        bucket_id="swing-indian",
        engaged_at=NOW - timedelta(minutes=900),
        now=NOW,
        max_dwell_minutes=120,
    )
    assert a.alert_signature == b.alert_signature == "halted"
    assert a.detail != b.detail  # the detail moves, the signature does not


def test_dwell_tolerates_a_naive_engaged_at():
    """engaged_at predates the tz-aware column on some rows; must not crash."""
    r = si.check_kill_switch_dwell(
        bucket_id="swing-indian",
        engaged_at=(NOW - timedelta(hours=9)).replace(tzinfo=None),
        now=NOW,
        max_dwell_minutes=120,
    )
    assert not r.ok
