"""
Session invariants — per-tick assertions that the live session is BEHAVING.

Breakers (``breakers.py``) answer one question: has equity fallen off a cliff?
That is the right question for a leveraged crypto sub-account, where drawdown,
liquidation distance and funding are the failure modes. It is the WRONG question
for the Indian equity buckets that went live in July 2026. Their failure modes
are procedural, and every one of them can happen at a perfectly healthy equity:

  * intraday-indian's 15:15 square-off lives inside the STRATEGY's exit
    (``gap_down_reversal.exits``), driven by the latest bar's timestamp. A stale
    feed, a tick error or a rejected exit each leave the position open — and on
    the CNC fallback (Decision 029, amended 2026-07-27) there is no broker
    auto-square-off net behind it. A leveraged intraday trade silently becomes
    an unwanted overnight delivery.
  * a bot-owned position can end a tick with no resting protective stop
    (Decision 022) when placement failed: equity is fine, the crash net is not
    there.
  * a sizing bug commits more than the bucket's capital budget long before it
    shows up as drawdown.
  * a single bucket's runner can wedge while the process heartbeat keeps
    beating — ``core/heartbeat.py`` is process-level, so one dead bucket is
    invisible to the dead-man's switch.

Each check returns an :class:`InvariantResult`. Enforcement is deliberately
capped at HALT: engage that bucket's kill switch, which per Decision 024 blocks
risk-INCREASING actions only — strategy exits, the stop sweep and the breakers
all keep running. Flattening stays where it already is, in ``enforcement.py``,
reachable only by a deterministic breaker trip. An invariant is an assertion
about process, not a view on the market, and must never close a position.

On the shared Dhan account (Decision 027) every check is scoped to bot-owned
quantities. The user's own manual positions are reported as a NOTICE and are
never acted on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select

from src.brokers.base import Broker, OpenOrder, PositionInfo
from src.core.alerts import note_alert_recovery, send_alert_dedup
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.heartbeat import last_beat, staleness
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    BrokerName,
    OrderStatus,
    SizingSnapshot,
    Trade,
)
from src.order_manager.ownership import bot_owned_quantities
from src.safety import kill_switch
from src.shared.market_calendar import IST, is_trading_day

_log = get_logger("safety.session_invariants")

# intraday-indian's strategy squares off at 15:15 IST; the session closes at
# 15:30. Asserting at 15:15 + grace leaves real minutes to act by hand while
# the market is still open, without racing the strategy's own exit.
SQUAREOFF_TIME = time(15, 15)


class Severity(StrEnum):
    """What enforcement is allowed to do about a violation.

    There is deliberately no FLATTEN level — see the module docstring.
    """

    NOTICE = "notice"  # Telegram only; never touches the kill switch
    HALT = "halt"  # engage the bucket's kill switch, then Telegram


@dataclass(frozen=True, slots=True)
class BucketWatch:
    """The per-bucket facts the invariants need, lifted out of buckets.yaml.

    Plain data rather than a ``Bucket`` object, matching how
    ``ensure_stop_protection`` takes ``stop_pct_by_bucket`` — it keeps every
    check unit-testable without loading YAML or touching a database.
    """

    bucket_id: str
    tick_interval_seconds: int
    # True when the bucket's product must be flat by the end of the session
    # (intraday-indian's INTRADAY/CNC-fallback). swing-indian holds MTF
    # positions for days and must never be square-off-checked.
    intraday: bool = False
    # capital_inr × leverage_max, or None to skip the ceiling check. None for
    # every crypto bucket: Delta positions are CONTRACT-denominated and priced
    # in USD, so qty × entry_price is neither a base-unit size nor rupees. The
    # caller owns that conversion; the check only compares like with like.
    notional_budget_inr: Decimal | None = None


@dataclass(frozen=True, slots=True)
class InvariantResult:
    name: str
    bucket_id: str
    ok: bool
    severity: Severity = Severity.NOTICE
    message: str = ""
    detail: dict = field(default_factory=dict)
    # Consecutive violating ticks required before enforcement acts. The sweep
    # runs every tick, so a single miss can be a race against a just-placed
    # order; a real failure persists.
    sustain_ticks: int = 1
    # What counts as "the same alert" for suppression. None derives it from
    # ``detail``, which is right when detail is an identity (which symbols are
    # uncovered). Set it explicitly when detail carries a value that moves on
    # its own — an age in seconds ticks every second, and re-alerting on that
    # would defeat the suppression entirely.
    alert_signature: str | None = None


# ---------------------------------------------------------------------------
# Holdings primitive
# ---------------------------------------------------------------------------
def effective_holdings(
    positions: list[PositionInfo],
    owned: dict[str, Decimal] | None,
) -> dict[str, Decimal]:
    """``{symbol: qty}`` the bot actually holds RIGHT NOW on the exchange. PURE.

    The intersection of two independent sources, and the intersection is the
    point. ``owned`` (our Trade ledger) alone goes stale the moment something
    closes a position without writing a SELL row — Dhan's own MIS auto-square-off
    does exactly that, and would otherwise leave a phantom holding that fails the
    square-off invariant forever. ``positions`` alone cannot tell the bot's rows
    from the user's on the shared Dhan account.

    ``owned=None`` means an exclusive account (every crypto sub-account,
    Decision 019): the whole position is the bot's.
    """
    out: dict[str, Decimal] = {}
    for pos in positions:
        if pos.side != "long" or pos.size <= 0:
            continue
        qty = pos.size if owned is None else min(pos.size, owned.get(pos.symbol, Decimal("0")))
        if qty > 0:
            out[pos.symbol] = qty
    return out


# ---------------------------------------------------------------------------
# Checks — pure. No DB, no broker, no clock of their own.
# ---------------------------------------------------------------------------
def check_squareoff(
    *,
    bucket: BucketWatch,
    holdings: dict[str, Decimal],
    now: datetime,
    grace_minutes: int,
) -> InvariantResult:
    """An intraday bucket must be flat once its square-off deadline passes.

    Fires from the deadline until the end of the trading day, and keeps firing:
    after 15:30 an un-squared MIS position is a worse problem, not a stale one.
    A halt cannot close it — the alert is the actionable part — but it does stop
    the bucket adding a second one on top.
    """
    name = "squareoff"
    if not bucket.intraday:
        return InvariantResult(name, bucket.bucket_id, ok=True)

    ist_now = now.astimezone(IST)
    if not is_trading_day(ist_now.date()):
        return InvariantResult(name, bucket.bucket_id, ok=True)

    deadline_minutes = SQUAREOFF_TIME.hour * 60 + SQUAREOFF_TIME.minute + grace_minutes
    now_minutes = ist_now.hour * 60 + ist_now.minute
    if now_minutes < deadline_minutes:
        return InvariantResult(name, bucket.bucket_id, ok=True)

    if not holdings:
        return InvariantResult(name, bucket.bucket_id, ok=True)

    symbols = ", ".join(f"{s} x{q}" for s, q in sorted(holdings.items()))
    return InvariantResult(
        name,
        bucket.bucket_id,
        ok=False,
        severity=Severity.HALT,
        message=(
            f"SQUARE-OFF FAILED: {bucket.bucket_id} still holds {symbols} at "
            f"{ist_now:%H:%M} IST (deadline {SQUAREOFF_TIME:%H:%M} + "
            f"{grace_minutes}m). These will carry OVERNIGHT as delivery unless "
            f"closed BY HAND now — the CNC fallback has no broker square-off."
        ),
        detail={"symbols": {s: str(q) for s, q in holdings.items()}},
    )


def check_stop_coverage(
    *,
    bucket_id: str,
    holdings: dict[str, Decimal],
    open_orders: list[OpenOrder],
    sustain_ticks: int,
    attached_stops: dict[str, Decimal] | None = None,
) -> InvariantResult:
    """Every bot-held position needs a resting reduce-only stop (Decision 022).

    Runs AFTER the sweep, so a gap here means the sweep tried and failed (or
    never planned one) — not that it hasn't got to it yet.

    ``attached_stops`` (Decision 034) are positions the VENUE protects via a
    stop leg carried on the entry order itself. They count as covered on their
    own evidence rather than via ``open_orders``: a super-order leg is only
    recognisable there if Dhan echoes our ``correlationId`` onto it, which is
    unverified. Relying on that would make this check HALT every bucket holding
    a super-order position — the invariant firing precisely because the
    stronger protection was used.
    """
    name = "stop_coverage"
    covered = set(attached_stops or {})
    for o in open_orders:
        if o.stop_price is None or o.unfilled_size <= 0:
            continue
        if o.reduce_only:
            # Provably ours (Dhan infers this from OUR correlationId).
            covered.add(o.symbol)
            continue
        # NOT provably ours — a stop the user placed by hand in the Dhan app
        # comes back with correlationId "NA". Ownership is the right test for
        # CANCELLING an order and the wrong one for this check: "is the
        # position protected" is a question about the market, not about who
        # placed the order. Without this branch a hand-placed stop leaves the
        # bucket halting every tick while the position is in fact protected.
        #
        # Two conditions keep it honest. SELL, because a resting BUY stop is an
        # entry trigger, not protection for a long (Indian equity is long-only;
        # crypto sub-accounts are exclusive, so no foreign order exists there).
        # And big enough to cover OUR holding, because on a shared account the
        # user may hold the same scrip — a stop sized for their 100 shares says
        # nothing about the bot's 15.
        held = holdings.get(o.symbol, Decimal("0"))
        if str(o.side).lower() == "sell" and held > 0 and o.unfilled_size >= held:
            covered.add(o.symbol)
    uncovered = sorted(set(holdings) - covered)
    if not uncovered:
        return InvariantResult(name, bucket_id, ok=True)

    return InvariantResult(
        name,
        bucket_id,
        ok=False,
        severity=Severity.HALT,
        message=(
            f"NO PROTECTIVE STOP on {bucket_id}: {', '.join(uncovered)} — the "
            f"sweep did not leave a resting reduce-only stop. These positions "
            f"are naked if the bot or VM dies."
        ),
        detail={"uncovered": uncovered},
        sustain_ticks=sustain_ticks,
    )


def check_notional_ceiling(
    *,
    bucket: BucketWatch,
    holdings: dict[str, Decimal],
    entry_prices: dict[str, Decimal],
    tolerance: Decimal,
) -> InvariantResult:
    """Committed notional must stay inside the bucket's capital × leverage.

    Catches a sizing bug at the moment it over-commits, which is well before it
    shows up as drawdown. Priced at ENTRY, not mark: the question is how much
    capital the allocator committed, not what the book is worth now.

    Skipped entirely when the bucket has no INR budget — see
    ``BucketWatch.notional_budget_inr``.
    """
    name = "notional_ceiling"
    if bucket.notional_budget_inr is None or bucket.notional_budget_inr <= 0:
        return InvariantResult(name, bucket.bucket_id, ok=True)

    committed = sum(
        (qty * entry_prices.get(sym, Decimal("0")) for sym, qty in holdings.items()),
        Decimal("0"),
    )
    ceiling = bucket.notional_budget_inr * tolerance
    if committed <= ceiling:
        return InvariantResult(name, bucket.bucket_id, ok=True)

    return InvariantResult(
        name,
        bucket.bucket_id,
        ok=False,
        severity=Severity.HALT,
        message=(
            f"NOTIONAL CEILING BREACHED on {bucket.bucket_id}: committed "
            f"Rs {committed:,.0f} vs ceiling Rs {ceiling:,.0f} "
            f"(budget Rs {bucket.notional_budget_inr:,.0f} x {tolerance} "
            f"tolerance). Suspect a sizing bug — halting entries."
        ),
        detail={"committed": str(committed), "ceiling": str(ceiling)},
    )


def check_reject_rate(
    *,
    bucket_id: str,
    rejected: int,
    threshold: int,
    window_minutes: int,
) -> InvariantResult:
    """Repeated broker rejections mean the order path is broken, not unlucky."""
    name = "reject_rate"
    if rejected < threshold:
        return InvariantResult(name, bucket_id, ok=True)
    return InvariantResult(
        name,
        bucket_id,
        ok=False,
        severity=Severity.HALT,
        message=(
            f"ORDER REJECTS on {bucket_id}: {rejected} rejected in the last "
            f"{window_minutes}m (threshold {threshold}). The order path is "
            f"failing — halting entries. Check the Dhan token and margin."
        ),
        detail={"rejected": rejected, "window_minutes": window_minutes},
    )


def check_bucket_liveness(
    *,
    bucket: BucketWatch,
    beat_at: datetime | None,
    now: datetime,
    stale_multiple: float,
) -> InvariantResult:
    """This bucket completed a pipeline pass within a few of its own cadences.

    The process-level dead-man's switch cannot see a single wedged bucket: the
    loop keeps beating for the others. NOTICE, not HALT — a bucket that isn't
    running is already not entering anything, so halting it adds nothing, and a
    false positive here would silently disable a healthy bucket.
    """
    name = "bucket_liveness"
    threshold = int(bucket.tick_interval_seconds * stale_multiple)
    stale, age = staleness(beat_at, now, threshold)
    if not stale:
        return InvariantResult(name, bucket.bucket_id, ok=True)

    age_str = f"{int(age)}s" if age is not None else "never"
    return InvariantResult(
        name,
        bucket.bucket_id,
        ok=False,
        severity=Severity.NOTICE,
        message=(
            f"BUCKET STALLED: {bucket.bucket_id} last completed a pass "
            f"{age_str} ago (threshold {threshold}s) while the session is "
            f"open. The process is alive — this bucket is not."
        ),
        detail={"age_seconds": age, "threshold_seconds": threshold},
        # age_seconds grows every tick; without a fixed signature a stalled
        # bucket would page forever instead of once.
        alert_signature="stalled",
    )


@dataclass(frozen=True, slots=True)
class ScanCoverage:
    """The funnel of one scanner pass, lifted from its SCANNER_RUN payload.

    ``configured`` is the symbol list in git, ``attempted`` what survived the
    pre-filters (F&O flag, circuit band), ``evaluated`` how many actually got
    fetched and screened, ``unevaluable`` how many of those produced no usable
    data. Each narrowing is a distinct failure mode; see ``check_scan_coverage``.
    """

    bucket_id: str
    scanner_id: str
    configured: int
    attempted: int
    evaluated: int
    unevaluable: int
    ts: datetime


def check_scan_coverage(
    *,
    coverage: ScanCoverage | None,
    bucket_id: str,
    unevaluable_ratio: float,
) -> InvariantResult:
    """The scanner actually LOOKED at the symbols it was configured to look at.

    Every other invariant here watches positions. This one watches perception,
    because on 2026-08-04/05 a dead Dhan token made both Indian buckets fetch
    nothing for two full trading days while every layer reported a quiet
    market: the audit log said ``0/0 crossed of 0 evaluated`` and the EOD
    journal said "Scanners ran 11 pass(es); nothing qualified." A scanner that
    is structurally incapable of firing is indistinguishable, from the outside,
    from a market that simply did not qualify — unless something asserts the
    difference. That is this check.

    Every outcome here is a NOTICE. That is deliberate, and it is the second
    design this check had — the first one halted, which was wrong:

    **A blind scanner cannot place a bad trade.** It evaluates nothing, so it
    produces no signals, so it enters nothing. The kill switch blocks
    risk-INCREASING actions (Decision 024), and a scanner that has gone blind
    has already stopped increasing risk all by itself. Halting it prevents
    nothing that is not already prevented. This is precisely the reasoning
    ``check_bucket_liveness`` states for its own NOTICE, and it applies here
    for the same reason: the danger of a blind scanner is not that it acts
    wrongly, it is that it stays SILENT while you believe it is working. The
    cure for silence is an alert, not a halt.

    And the halt had a real cost. The kill switch only ever clears by hand
    (``kill_switch.disengage`` has exactly one caller, the dashboard button),
    so a six-second token blip during the 15:15 bin — which happened on
    2026-08-07 — would have halted the bucket overnight and skipped the next
    morning's session until someone noticed and clicked. Trading a two-day
    silent outage for an unattended overnight stop is not an improvement.

    So: say it loudly, say it repeatedly while it lasts, and leave the bucket
    running so it resumes the moment the data comes back.

    ``coverage is None`` means no scan has been recorded yet in the window;
    that is silence, not blindness, and ``check_bucket_liveness`` already owns
    it. Returning OK here keeps the two checks from double-reporting one fault.
    """
    name = "scan_coverage"
    if coverage is None:
        return InvariantResult(name, bucket_id, ok=True)

    detail = {
        "scanner": coverage.scanner_id,
        "configured": coverage.configured,
        "attempted": coverage.attempted,
        "evaluated": coverage.evaluated,
        "unevaluable": coverage.unevaluable,
    }

    if coverage.configured > 0 and coverage.attempted == 0:
        return InvariantResult(
            name,
            bucket_id,
            ok=False,
            severity=Severity.NOTICE,
            message=(
                f"SCANNER BLIND: {coverage.scanner_id} attempted 0 of "
                f"{coverage.configured} configured symbols. The universe "
                f"collapsed before the scan ran — check the Dhan scrip master "
                f"and the F&O/circuit filters. The bucket is still running, "
                f"but it is looking at nothing."
            ),
            detail=detail,
            alert_signature="blind:universe",
        )

    if coverage.attempted > 0 and coverage.evaluated == 0:
        return InvariantResult(
            name,
            bucket_id,
            ok=False,
            severity=Severity.NOTICE,
            message=(
                f"SCANNER BLIND: {coverage.scanner_id} evaluated 0 of "
                f"{coverage.attempted} symbols — every fetch failed. This is "
                f"NOT a quiet market. Check the Dhan token (a dead token 401s "
                f"every call). No signal can be found until this clears."
            ),
            detail=detail,
            alert_signature="blind:fetch",
        )

    if (
        coverage.evaluated > 0
        and coverage.unevaluable / coverage.evaluated >= unevaluable_ratio
    ):
        pct = 100.0 * coverage.unevaluable / coverage.evaluated
        return InvariantResult(
            name,
            bucket_id,
            ok=False,
            severity=Severity.NOTICE,
            message=(
                f"SCANNER DEGRADED: {coverage.scanner_id} could not use data "
                f"for {coverage.unevaluable}/{coverage.evaluated} symbols "
                f"({pct:.0f}%). Signals from this pass are not trustworthy."
            ),
            detail=detail,
            alert_signature="degraded",
        )

    return InvariantResult(name, bucket_id, ok=True)


def check_signal_delivery(
    *,
    bucket_id: str,
    lost: list[str],
    window_minutes: int,
) -> InvariantResult:
    """A signal the strategy DID produce reached the broker, or you hear why.

    ``scan_coverage`` asserts the bot looked. This asserts that what it saw
    survived the trip to an order — the gap between them is where 2026-08-07's
    real trade died. That scan was perfectly healthy: 94 symbols evaluated,
    BLUESTARCO found at -6.58% dislocation, Kelly approved at Rs 40,000. Then
    ``/v2/marketfeed/quote`` returned 401 on a dead token, the sizer had no
    price to turn rupees into shares, and it filed the miss as
    ``skipped_other`` — the same code it uses for "I decided not to". The
    signal was retried 38 times over an hour, aged out of its bin, and the
    only durable trace was a sizing_snapshot row nobody reads.

    NOTICE, for the same reason ``scan_coverage`` is: nothing here is a bad
    trade to prevent, it is a good trade already lost. A halt cannot recover
    it and would only stop the NEXT one, which is the opposite of what you
    want. The alert is the whole product.

    Only counts skips whose reason is the broker-fetch failure. An allocator
    that declines on dedup, regime or budget is working exactly as designed
    and must never page anyone.
    """
    name = "signal_delivery"
    if not lost:
        return InvariantResult(name, bucket_id, ok=True)

    return InvariantResult(
        name,
        bucket_id,
        ok=False,
        severity=Severity.NOTICE,
        message=(
            f"SIGNAL LOST TO DATA: {bucket_id} produced entry signal(s) for "
            f"{', '.join(sorted(set(lost)))} in the last {window_minutes}m "
            f"that could NOT be sized — the broker price fetch failed, so "
            f"there was no way to convert the Kelly notional into a quantity. "
            f"These are missed trades, not declined ones. Check the Dhan token."
        ),
        detail={"lost": sorted(set(lost))},
        # Keyed on WHICH symbols, so a new lost signal pages even while an
        # earlier one is still unresolved.
        alert_signature=",".join(sorted(set(lost))),
    )


def check_foreign_positions(
    *,
    account_ref: str,
    positions: list[PositionInfo],
    owned: dict[str, Decimal] | None,
) -> InvariantResult:
    """Report account positions the bot did not open. NOTICE — never acted on.

    On 2026-07-22 the stop sweep tried to manage the user's NIFTY options
    because it read the account rather than the bot's own order flow. Ownership
    scoping fixed that; this makes the scoping VISIBLE, so a wrong attribution
    shows up as a line in Telegram instead of as a surprise order.
    """
    name = "foreign_positions"
    if owned is None:
        return InvariantResult(name, account_ref, ok=True)

    foreign = [
        p.symbol
        for p in positions
        if p.size > 0 and p.size > owned.get(p.symbol, Decimal("0"))
    ]
    if not foreign:
        return InvariantResult(name, account_ref, ok=True)

    return InvariantResult(
        name,
        account_ref,
        ok=False,
        severity=Severity.NOTICE,
        message=(
            f"[{account_ref}] positions the bot does NOT own (yours, left "
            f"alone): {', '.join(sorted(foreign))}"
        ),
        detail={"foreign": sorted(foreign)},
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class InvariantThresholds:
    """Tunables, all sourced from ``Settings`` by the caller."""

    squareoff_grace_minutes: int = 5
    stop_coverage_sustain_ticks: int = 2
    notional_tolerance: Decimal = Decimal("1.10")
    reject_window_minutes: int = 15
    reject_max: int = 3
    bucket_stale_multiple: float = 3.0
    # How far back to look for the scan being asserted on. Wide enough to span
    # swing-indian's hourly bins, so the check keeps holding between them
    # rather than going quiet for 55 minutes of every hour.
    scan_lookback_minutes: int = 90
    # Fraction of evaluated symbols that may be unusable before it's a NOTICE.
    scan_unevaluable_ratio: float = 0.9
    # How far back check_signal_delivery looks for signals lost to a data
    # failure. Comfortably wider than one swing-indian 1h bin, so a miss stays
    # visible for the rest of the bin it happened in rather than for one tick.
    signal_lost_window_minutes: int = 90


def count_recent_rejects(
    *,
    broker_name: BrokerName,
    bucket_id: str,
    since: datetime,
) -> int:
    """REJECTED trade rows for one bucket since ``since``."""
    with session_scope() as session:
        return len(
            session.execute(
                select(Trade.id).where(
                    Trade.broker == broker_name,
                    Trade.bucket_id == bucket_id,
                    Trade.status == OrderStatus.REJECTED,
                    Trade.created_at >= since,
                )
            )
            .scalars()
            .all()
        )


def last_scan_coverage(*, bucket_id: str, since: datetime) -> ScanCoverage | None:
    """Funnel of the most recent SCANNER_RUN for one bucket, or None.

    A bucket can own several named scanner sets (Decision 026), whose audit
    rows are namespaced ``<bucket>:<name>`` — intraday-indian runs both
    ``intraday-indian`` and ``intraday-indian:broad``. Matching the bare id
    alone would silently exempt every named set from the check, so the prefix
    is matched too.

    Rows written before this payload existed carry no ``attempted``; they are
    reported as None rather than guessed at, so an old row cannot manufacture a
    halt from data it never recorded.
    """
    with session_scope() as session:
        row = session.execute(
            select(AuditLog)
            .where(
                AuditLog.event_type == AuditEventType.SCANNER_RUN,
                AuditLog.ts >= since,
                (AuditLog.strategy_id == bucket_id)
                | (AuditLog.strategy_id.startswith(f"{bucket_id}:")),
            )
            .order_by(AuditLog.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return coverage_from_payload(
            bucket_id=bucket_id,
            scanner_id=row.strategy_id or bucket_id,
            payload=row.payload,
            ts=row.ts,
        )


def coverage_from_payload(
    *,
    bucket_id: str,
    scanner_id: str,
    payload: dict | None,
    ts: datetime,
) -> ScanCoverage | None:
    """SCANNER_RUN payload → ScanCoverage, or None if it cannot be judged. PURE.

    Returning None for a payload without ``attempted`` is a SAFETY property,
    not a convenience: this check can halt a live bucket, and the only evidence
    it halts on is these counts. Scanner paths that never record them — the
    crypto ``run_scan`` engine, and every row written before 2026-08-07 —
    would otherwise default to zero and read as "looked at nothing", halting
    buckets that are working perfectly. Absent must mean unknown.
    """
    payload = payload or {}
    if "attempted" not in payload:
        return None
    try:
        return ScanCoverage(
            bucket_id=bucket_id,
            scanner_id=scanner_id,
            configured=int(payload.get("configured", 0)),
            attempted=int(payload["attempted"]),
            evaluated=int(payload.get("evaluated", 0)),
            unevaluable=int(payload.get("unevaluable", 0)),
            ts=ts,
        )
    except (TypeError, ValueError):
        # A malformed count is unknown, not zero — same reasoning as above.
        _log.warning(
            "scan_coverage_payload_unparseable",
            scanner_id=scanner_id,
            payload=payload,
        )
        return None


def signals_lost_to_data(*, bucket_id: str, since: datetime) -> list[str]:
    """Symbols this bucket sized-and-dropped on a broker price failure.

    Matches the sizer's canonical reason constant rather than a substring, so
    the two cannot drift apart silently.
    """
    from src.shared.allocator.sizer import PRICE_FETCH_FAILED_REASON

    with session_scope() as session:
        return list(
            session.execute(
                select(SizingSnapshot.symbol)
                .where(
                    SizingSnapshot.bucket_id == bucket_id,
                    SizingSnapshot.ts >= since,
                    SizingSnapshot.reason == PRICE_FETCH_FAILED_REASON,
                )
                .distinct()
            )
            .scalars()
            .all()
        )


def bucket_service(bucket_id: str) -> str:
    """Heartbeat ``service`` name for one bucket's per-pass liveness row."""
    return f"bucket:{bucket_id}"


def run_session_invariants(
    *,
    account_ref: str,
    watches: list[BucketWatch],
    broker: Broker,
    broker_name: BrokerName,
    thresholds: InvariantThresholds,
    clock: Clock | None = None,
    shared_account: bool = False,
    check_liveness: bool = True,
    attached_stops_enabled: bool = False,
) -> list[InvariantResult]:
    """Evaluate every invariant for one account. Reads, never writes.

    ``positions`` and ``open_orders`` are fetched ONCE per account and shared
    across buckets — Dhan is rate-limited, and this runs on the 60s loop.

    ``check_liveness`` is False outside an open session: a bucket that is
    correctly idle overnight is not stalled.
    """
    clk = clock or RealClock()
    now = clk.now()
    results: list[InvariantResult] = []

    positions = broker.get_positions()
    open_orders = broker.get_open_orders()
    entry_prices = {p.symbol: p.entry_price for p in positions}
    reject_since = now - timedelta(minutes=thresholds.reject_window_minutes)

    # Decision 034: stops the venue carries on the entry order itself. Fetched
    # per account alongside the other two reads. Fail-soft to empty here — the
    # opposite of the sweep's fail-closed — because this is an OBSERVER: an
    # empty answer makes stop_coverage shout about positions that are in fact
    # protected, which is noisy but safe, whereas skipping the check would
    # silence the alarm that a genuinely naked position depends on.
    # Gated on the feature, not the capability — see the same guard in
    # ``ensure_stop_protection``. With the feature off there are no legs to
    # find, so the request would be pure cost on a rate-limited account.
    attached_stops: dict[str, Decimal] = {}
    if attached_stops_enabled and broker.supports_attached_stop():
        try:
            attached_stops = broker.attached_stop_triggers()
        except Exception:
            _log.warning("invariant_attached_stop_lookup_failed", exc_info=True)

    account_owned: dict[str, Decimal] | None = None
    if shared_account:
        with session_scope() as session:
            account_owned = bot_owned_quantities(
                session,
                broker_name=broker_name,
                bucket_ids=[w.bucket_id for w in watches],
                now=now,
            )

    results.append(
        check_foreign_positions(
            account_ref=account_ref, positions=positions, owned=account_owned
        )
    )

    for watch in watches:
        if shared_account:
            with session_scope() as session:
                owned = bot_owned_quantities(
                    session,
                    broker_name=broker_name,
                    bucket_ids=[watch.bucket_id],
                    now=now,
                )
        else:
            # Exclusive sub-account (Decision 019): one bucket, all of it ours.
            owned = None

        holdings = effective_holdings(positions, owned)

        results.append(
            check_squareoff(
                bucket=watch,
                holdings=holdings,
                now=now,
                grace_minutes=thresholds.squareoff_grace_minutes,
            )
        )
        results.append(
            check_stop_coverage(
                bucket_id=watch.bucket_id,
                holdings=holdings,
                open_orders=open_orders,
                sustain_ticks=thresholds.stop_coverage_sustain_ticks,
                attached_stops=attached_stops,
            )
        )
        results.append(
            check_notional_ceiling(
                bucket=watch,
                holdings=holdings,
                entry_prices=entry_prices,
                tolerance=thresholds.notional_tolerance,
            )
        )
        results.append(
            check_reject_rate(
                bucket_id=watch.bucket_id,
                rejected=count_recent_rejects(
                    broker_name=broker_name,
                    bucket_id=watch.bucket_id,
                    since=reject_since,
                ),
                threshold=thresholds.reject_max,
                window_minutes=thresholds.reject_window_minutes,
            )
        )
        if check_liveness:
            results.append(
                check_bucket_liveness(
                    bucket=watch,
                    beat_at=last_beat(bucket_service(watch.bucket_id)),
                    now=now,
                    stale_multiple=thresholds.bucket_stale_multiple,
                )
            )
            # Gated on check_liveness for the same reason: outside an open
            # session a bucket is correctly not scanning, and asserting on
            # perception when nothing is meant to be perceived would halt every
            # bucket overnight.
            results.append(
                check_scan_coverage(
                    coverage=last_scan_coverage(
                        bucket_id=watch.bucket_id,
                        since=now
                        - timedelta(minutes=thresholds.scan_lookback_minutes),
                    ),
                    bucket_id=watch.bucket_id,
                    unevaluable_ratio=thresholds.scan_unevaluable_ratio,
                )
            )
            results.append(
                check_signal_delivery(
                    bucket_id=watch.bucket_id,
                    lost=signals_lost_to_data(
                        bucket_id=watch.bucket_id,
                        since=now
                        - timedelta(minutes=thresholds.signal_lost_window_minutes),
                    ),
                    window_minutes=thresholds.signal_lost_window_minutes,
                )
            )

    return results


# Consecutive violating ticks per (bucket, check). Reset the moment a check
# passes, so a transient race never accumulates toward a halt.
_streaks: dict[str, int] = {}

# Digest of the detail last ALERTED per key, so a steady-state violation pages
# once rather than every hour. Cleared when the check passes.
_last_alerted: dict[str, str] = {}


def reset_streaks(key: str | None = None) -> None:
    """Clear violation streaks — one key, or all of them (tests, restart)."""
    if key is None:
        _streaks.clear()
        _last_alerted.clear()
    else:
        _streaks.pop(key, None)
        _last_alerted.pop(key, None)


def audit_violation(
    res: InvariantResult, *, streak: int, would_halt: bool, enforcing: bool
) -> None:
    """Record the violation in ``audit_log``. Best-effort, never raises.

    Until this existed an invariant left NO durable trace. It logged to stdout
    and paged Telegram, and reached Postgres only if it escalated to HALT —
    which writes ``KILL_SWITCH_FLIPPED`` from ``kill_switch.engage``. Since the
    EOD journal (``reporting/eod.py``) is built entirely from audit rows, every
    violation that cleared before halting anything was reported as "nothing
    tripped": a Telegram ping, and then silence in the permanent record.

    Written on the SAME condition as the alert — first sighting, and again only
    when what it says changes. A row per tick would reproduce, in the audit log,
    exactly the steady-state flood that ``foreign_positions`` caused in Telegram
    on 2026-07-28.

    Swallows its own failures on purpose. This runs inside the safety loop, and
    a monitor that killed the checks it exists to record would be worse than no
    monitor — the same reasoning that keeps the EOD report off the broker API.
    """
    try:
        with session_scope() as session:
            session.add(
                AuditLog(
                    strategy_id=res.bucket_id,
                    event_type=AuditEventType.INVARIANT_VIOLATED,
                    message=f"{res.name}: {res.message}",
                    payload={
                        "invariant": res.name,
                        "bucket_id": res.bucket_id,
                        "severity": res.severity.value,
                        "streak": streak,
                        "sustain_ticks": res.sustain_ticks,
                        "would_halt": would_halt,
                        "enforcing": enforcing,
                        "detail": json.loads(
                            json.dumps(res.detail, sort_keys=True, default=str)
                        ),
                    },
                )
            )
    except Exception:
        _log.error("invariant_audit_write_failed", invariant=res.name, exc_info=True)


def enforce_session_invariants(
    results: list[InvariantResult],
    *,
    clock: Clock | None = None,
    enforcing: bool = True,
) -> list[InvariantResult]:
    """Alert on violations and halt the offending bucket. Returns those halted.

    HALT engages that bucket's kill switch and nothing more. Per Decision 024
    the bucket's strategy exits, its stop sweep and its breakers all keep
    running while killed — only new risk is blocked. Recovery is manual from
    the dashboard, matching how a breaker trip already behaves.

    ``enforcing=False`` is OBSERVE-ONLY: every check still runs, every
    violation still logs and pages — prefixed so it is unmistakable — but the
    kill switch is never touched, and the returned list is empty. No invariant
    has yet fired against a real session, so this is how a new one earns the
    right to halt a live bucket: watch it agree with reality for a few
    sessions first. A false positive here costs a Telegram message instead of
    a halted bucket.
    """
    halted: list[InvariantResult] = []

    for res in results:
        key = f"invariant:{res.name}:{res.bucket_id}"
        if res.ok:
            _streaks.pop(key, None)
            _last_alerted.pop(key, None)
            note_alert_recovery(key, f"[{res.bucket_id}] {res.name} OK again")
            continue

        streak = _streaks.get(key, 0) + 1
        _streaks[key] = streak
        would_halt = res.severity is Severity.HALT and streak >= res.sustain_ticks
        _log.warning(
            "session_invariant_violated",
            invariant=res.name,
            bucket_id=res.bucket_id,
            severity=res.severity.value,
            streak=streak,
            enforcing=enforcing,
            would_halt=would_halt,
            **res.detail,
        )
        prefix = (
            "[OBSERVE-ONLY, would have HALTED] "
            if would_halt and not enforcing
            else ""
        )
        # Alert on the CONTENT changing, not on the condition merely persisting.
        #
        # send_alert_dedup's window re-arms hourly, which is right for a
        # transient error that keeps recurring and wrong for a steady state.
        # foreign_positions is permanently violated whenever the user holds
        # anything on the shared Dhan account, so from 2026-07-28 it paged 3x an
        # hour — ~72 messages a day about positions the bot is correctly
        # ignoring. That is noise on its own, and worse, it buries the alerts
        # this system exists to deliver.
        #
        # Keying on a digest of the detail means: page when the condition
        # appears, page again only when what it says changes, otherwise stay
        # quiet until it clears (which re-arms via note_alert_recovery above).
        # ``would_halt`` is part of the signature: escalating from "seen once"
        # to "would have HALTED" is new news even when the content is identical.
        signature = res.alert_signature
        if signature is None:
            signature = json.dumps(res.detail, sort_keys=True, default=str)
        digest = f"{would_halt}|{signature}"
        if _last_alerted.get(key) != digest:
            _last_alerted[key] = digest
            send_alert_dedup(key, f"{prefix}[{res.bucket_id}] {res.message}")
            # Same condition as the page, so the permanent record and the
            # Telegram thread agree on what happened and when.
            audit_violation(
                res, streak=streak, would_halt=would_halt, enforcing=enforcing
            )

        if not would_halt or not enforcing:
            continue
        if kill_switch.is_engaged(res.bucket_id):
            continue  # already halted — don't re-engage or re-page every tick

        kill_switch.engage(
            f"session invariant {res.name}: {res.message}",
            strategy_id=res.bucket_id,
            engaged_by="session_invariants",
            clock=clock,
        )
        halted.append(res)

    return halted
