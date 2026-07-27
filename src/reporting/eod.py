"""
End-of-day session postmortem (Decision 033, Phase 7a Tier 3).

The nightly export (``core/export.py``) already dumps the trade ledger to
Parquet at 00:30 UTC — 06:00 IST, the morning AFTER. That is an archive, not a
report: it says what was traded and nothing about whether the session behaved.

This module answers the questions you actually have at 15:45:

  * what did each bucket make, and on what?
  * which signals did NOT trade, and why? (``sizing_snapshot`` — the forensic
    record already exists, nothing has ever read it back)
  * did anything trip — invariants, breakers, kill switch, rejects?
  * what is carried overnight, and is it stop-protected?
  * is the live edge tracking the backtest, or drifting?

Structure mirrors the rest of the codebase: pure builders that take
already-loaded rows and return plain data, plus a thin gather/persist layer.
The renderers are pure string functions, so the whole report is testable
without a database.

HARD CONSTRAINT (Decision 033): reads Postgres ONLY. It must never call the
Dhan API — a second session evicts the bot's token, and a monitor that caused
the outage it exists to detect would be worse than no monitor. Every number
below comes from rows the reconciler already mirrors.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.db import session_scope
from src.core.models import (
    AuditEventType,
    AuditLog,
    OrderSide,
    OrderStatus,
    Position,
    PositionSide,
    SessionReport,
    SizingSnapshot,
    Trade,
)
from src.order_manager.pnl import pnl_pct
from src.shared.market_calendar import IST

_ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TradeLine:
    """One order the bot placed today, flattened for display."""

    bucket_id: str
    strategy_name: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal | None
    status: str
    fees: Decimal
    realized: Decimal | None
    at: datetime | None

    @property
    def notional(self) -> Decimal:
        return self.quantity * (self.price or _ZERO)


@dataclass(frozen=True, slots=True)
class SkipLine:
    """A signal the allocator saw and declined, with the reason it gave."""

    bucket_id: str
    strategy_name: str
    symbol: str
    decision: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class CarriedPosition:
    bucket_id: str
    symbol: str
    quantity: Decimal
    entry_price: Decimal | None

    @property
    def notional(self) -> Decimal:
        return self.quantity * (self.entry_price or _ZERO)


@dataclass(frozen=True, slots=True)
class BucketSection:
    bucket_id: str
    entries: list[TradeLine] = field(default_factory=list)
    exits: list[TradeLine] = field(default_factory=list)
    rejects: list[TradeLine] = field(default_factory=list)
    skips: list[SkipLine] = field(default_factory=list)
    carried: list[CarriedPosition] = field(default_factory=list)
    realized: Decimal = _ZERO
    fees: Decimal = _ZERO

    @property
    def traded(self) -> bool:
        return bool(self.entries or self.exits or self.rejects)


@dataclass(frozen=True, slots=True)
class Report:
    session_date: date_
    buckets: list[BucketSection]
    events: list[str]
    quiet: bool  # nothing traded, nothing tripped, nothing carried

    @property
    def realized(self) -> Decimal:
        return sum((b.realized for b in self.buckets), _ZERO)

    @property
    def fees(self) -> Decimal:
        return sum((b.fees for b in self.buckets), _ZERO)


# ---------------------------------------------------------------------------
# Builders — pure. Take rows, return shapes.
# ---------------------------------------------------------------------------
def _realized_of(trade: Trade) -> Decimal | None:
    """Realized P&L the reconciler stamped on an exit, if any."""
    extra = trade.extra or {}
    raw = extra.get("realized_pnl")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None


def to_trade_line(trade: Trade) -> TradeLine:
    return TradeLine(
        bucket_id=trade.bucket_id or trade.strategy_id,
        strategy_name=trade.strategy_name or "—",
        symbol=trade.symbol,
        side=trade.side.value,
        quantity=trade.quantity or _ZERO,
        price=trade.price,
        status=trade.status.value,
        fees=trade.fees or _ZERO,
        realized=_realized_of(trade),
        at=trade.filled_at or trade.submitted_at or trade.created_at,
    )


def build_sections(
    trades: list[Trade],
    skips: list[SizingSnapshot],
    positions: list[Position],
) -> list[BucketSection]:
    """Group the day's rows into one section per bucket. PURE.

    A bucket appears if it did ANYTHING — traded, was declined, or is carrying.
    A bucket that saw no signal at all is silent rather than noise.
    """
    bucket_ids: set[str] = set()
    lines = [to_trade_line(t) for t in trades]
    skip_lines = [
        SkipLine(
            bucket_id=s.bucket_id,
            strategy_name=s.strategy_name,
            symbol=s.symbol,
            decision=s.decision.value,
            reason=s.reason,
        )
        for s in skips
    ]
    carried = [
        CarriedPosition(
            bucket_id=p.bucket_id or p.strategy_id,
            symbol=p.symbol,
            quantity=p.quantity,
            entry_price=p.entry_price,
        )
        for p in positions
        if p.side != PositionSide.FLAT and p.quantity > 0
    ]
    for group in (lines, skip_lines, carried):
        bucket_ids.update(x.bucket_id for x in group)

    sections: list[BucketSection] = []
    for bucket_id in sorted(bucket_ids):
        mine = [t for t in lines if t.bucket_id == bucket_id]
        rejects = [t for t in mine if t.status == OrderStatus.REJECTED.value]
        live = [t for t in mine if t.status != OrderStatus.REJECTED.value]
        # Indian strategies are long-only, so BUY is an entry and SELL an exit.
        entries = [t for t in live if t.side == OrderSide.BUY.value]
        exits = [t for t in live if t.side == OrderSide.SELL.value]
        sections.append(
            BucketSection(
                bucket_id=bucket_id,
                entries=entries,
                exits=exits,
                rejects=rejects,
                skips=[s for s in skip_lines if s.bucket_id == bucket_id],
                carried=[c for c in carried if c.bucket_id == bucket_id],
                realized=sum(
                    (t.realized for t in exits if t.realized is not None), _ZERO
                ),
                fees=sum((t.fees for t in live), _ZERO),
            )
        )
    return sections


def build_events(rows: list[AuditLog]) -> list[str]:
    """One line per thing that tripped today. PURE."""
    return [
        f"{r.created_at:%H:%M} · {r.event_type.value} · {r.message}"
        for r in rows
    ]


def build_report(
    *,
    session_date: date_,
    trades: list[Trade],
    skips: list[SizingSnapshot],
    positions: list[Position],
    events: list[AuditLog],
) -> Report:
    sections = build_sections(trades, skips, positions)
    event_lines = build_events(events)
    quiet = not event_lines and not any(
        s.traded or s.carried or s.skips for s in sections
    )
    return Report(
        session_date=session_date,
        buckets=sections,
        events=event_lines,
        quiet=quiet,
    )


# ---------------------------------------------------------------------------
# Renderers — pure strings
# ---------------------------------------------------------------------------
def _money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"Rs {value:,.2f}"


def _signed(value: Decimal) -> str:
    return f"{'+' if value >= 0 else ''}{value:,.2f}"


def render_digest(report: Report) -> str:
    """The 10-line Telegram version. Readable on a phone, no tables."""
    head = f"EOD {report.session_date:%a %d %b %Y}"
    if report.quiet:
        return f"{head}\nNo trades, no signals, nothing tripped. Quiet day."

    out = [head, f"Realized Rs {_signed(report.realized)} (fees {_money(report.fees)})"]
    for sec in report.buckets:
        bits = []
        if sec.entries:
            bits.append(f"{len(sec.entries)} in")
        if sec.exits:
            bits.append(f"{len(sec.exits)} out")
        if sec.rejects:
            bits.append(f"{len(sec.rejects)} REJECTED")
        if sec.carried:
            bits.append(f"{len(sec.carried)} carried")
        if not bits:
            bits.append(f"{len(sec.skips)} signals, none taken")
        out.append(
            f"· {sec.bucket_id}: {', '.join(bits)} | Rs {_signed(sec.realized)}"
        )

    carried = [c for sec in report.buckets for c in sec.carried]
    if carried:
        out.append(
            f"OVERNIGHT: {', '.join(f'{c.symbol} x{c.quantity}' for c in carried)}"
        )
    if report.events:
        out.append(f"EVENTS ({len(report.events)}):")
        out.extend(f"  {line}" for line in report.events[:5])
        if len(report.events) > 5:
            out.append(f"  …and {len(report.events) - 5} more — see /journal")
    return "\n".join(out)


def _trade_table(lines: list[TradeLine]) -> list[str]:
    out = [
        "| time | symbol | strategy | qty | price | notional | fees | realized |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in lines:
        when = f"{t.at.astimezone(IST):%H:%M}" if t.at else "—"
        out.append(
            f"| {when} | {t.symbol} | {t.strategy_name} | {t.quantity} | "
            f"{_money(t.price)} | {_money(t.notional)} | {_money(t.fees)} | "
            f"{_money(t.realized)} |"
        )
    return out


def render_markdown(report: Report) -> str:
    """The full journal entry — one file per trading date."""
    out = [
        f"# Session journal — {report.session_date:%A %d %B %Y}",
        "",
        "*Generated from Postgres by `src/reporting/eod.py` "
        "(Decision 033, Tier 3).*",
        "",
    ]

    if report.quiet:
        out += [
            "No trades, no declined signals, nothing tripped, nothing carried.",
            "",
            "A quiet day is a real outcome, not a missing report — both live "
            "strategies wait for a specific setup and most days do not offer "
            "one.",
            "",
        ]
        return "\n".join(out)

    out += [
        "## Summary",
        "",
        f"- **Realized:** Rs {_signed(report.realized)}",
        f"- **Fees:** {_money(report.fees)}",
        f"- **Buckets active:** {', '.join(s.bucket_id for s in report.buckets)}",
        "",
    ]

    for sec in report.buckets:
        out += [f"## {sec.bucket_id}", ""]
        realized_pct = pnl_pct(
            sec.realized, sum((t.notional for t in sec.exits), _ZERO)
        )
        out.append(
            f"Realized Rs {_signed(sec.realized)}"
            + (f" ({realized_pct:.2f}% of exited notional)" if realized_pct else "")
            + f" · fees {_money(sec.fees)}"
        )
        out.append("")

        if sec.entries:
            out += ["### Entries", "", *_trade_table(sec.entries), ""]
        if sec.exits:
            out += ["### Exits", "", *_trade_table(sec.exits), ""]
        if sec.rejects:
            out += [
                "### Rejected orders",
                "",
                "The broker refused these. Repeated rejects trip the "
                "`reject_rate` invariant.",
                "",
                *_trade_table(sec.rejects),
                "",
            ]

        if sec.skips:
            out += [
                "### Signals seen but not taken",
                "",
                "Straight from `sizing_snapshot` — the answer to \"why didn't "
                "it trade today?\".",
                "",
                "| reason | count | symbols |",
                "|---|---|---|",
            ]
            by_reason: dict[str, list[str]] = {}
            for s in sec.skips:
                by_reason.setdefault(s.decision, []).append(s.symbol)
            for reason, syms in sorted(by_reason.items()):
                shown = ", ".join(sorted(syms)[:10])
                if len(syms) > 10:
                    shown += f", …+{len(syms) - 10}"
                out.append(f"| `{reason}` | {len(syms)} | {shown} |")
            out.append("")

        if sec.carried:
            total = sum((c.notional for c in sec.carried), _ZERO)
            out += [
                "### Carried overnight",
                "",
                f"{len(sec.carried)} position(s), {_money(total)} at entry. "
                f"Gap risk until the next open; the protective stop rests on "
                f"the exchange (Decision 022) and survives a bot or VM outage.",
                "",
                "| symbol | qty | entry | notional |",
                "|---|---|---|---|",
            ]
            for c in sec.carried:
                out.append(
                    f"| {c.symbol} | {c.quantity} | {_money(c.entry_price)} | "
                    f"{_money(c.notional)} |"
                )
            out.append("")

    if report.events:
        out += [
            "## Events",
            "",
            "Breakers, kill-switch flips, regime changes and invariant "
            "violations recorded in `audit_log`.",
            "",
        ]
        out += [f"- {line}" for line in report.events]
        out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Gather + persist
# ---------------------------------------------------------------------------
_REPORTABLE_EVENTS = (
    AuditEventType.BREAKER_TRIPPED,
    AuditEventType.KILL_SWITCH_FLIPPED,
    AuditEventType.REGIME_CHANGE,
    AuditEventType.DRIFT_ALERT,
    AuditEventType.RECONCILE_DIFF,
)


def ist_day_bounds(session_date: date_) -> tuple[datetime, datetime]:
    """UTC-comparable [start, end) for one IST calendar day."""
    start = datetime.combine(session_date, datetime.min.time(), tzinfo=IST)
    return start, start + timedelta(days=1)


def gather(session_date: date_) -> Report:
    """Load one IST day out of Postgres and build the report."""
    start, end = ist_day_bounds(session_date)
    with session_scope() as session:
        trades = list(
            session.execute(
                select(Trade)
                .where(Trade.created_at >= start, Trade.created_at < end)
                .order_by(Trade.created_at)
            ).scalars()
        )
        skips = list(
            session.execute(
                select(SizingSnapshot)
                .where(SizingSnapshot.ts >= start, SizingSnapshot.ts < end)
                .order_by(SizingSnapshot.ts)
            ).scalars()
        )
        # Positions are current state, not a dated row: what is open once the
        # session has closed IS what is carried overnight.
        positions = list(
            session.execute(
                select(Position).where(Position.side != PositionSide.FLAT)
            ).scalars()
        )
        events = list(
            session.execute(
                select(AuditLog)
                .where(
                    AuditLog.created_at >= start,
                    AuditLog.created_at < end,
                    AuditLog.event_type.in_(_REPORTABLE_EVENTS),
                )
                .order_by(AuditLog.created_at)
            ).scalars()
        )
        return build_report(
            session_date=session_date,
            trades=trades,
            skips=skips,
            positions=positions,
            events=events,
        )


def payload_of(report: Report) -> dict:
    """The structured numbers behind the prose, for later analysis."""
    return {
        "realized": str(report.realized),
        "fees": str(report.fees),
        "quiet": report.quiet,
        "events": len(report.events),
        "buckets": {
            s.bucket_id: {
                "entries": len(s.entries),
                "exits": len(s.exits),
                "rejects": len(s.rejects),
                "carried": len(s.carried),
                "realized": str(s.realized),
                "fees": str(s.fees),
                "skip_reasons": dict(Counter(x.decision for x in s.skips)),
            }
            for s in report.buckets
        },
    }


def store(report: Report) -> None:
    """UPSERT the report for its date — a re-run overwrites, never duplicates."""
    with session_scope() as session:
        row = session.execute(
            select(SessionReport).where(
                SessionReport.session_date == report.session_date
            )
        ).scalar_one_or_none()
        digest, markdown = render_digest(report), render_markdown(report)
        if row is None:
            session.add(
                SessionReport(
                    session_date=report.session_date,
                    digest=digest,
                    markdown=markdown,
                    payload=payload_of(report),
                )
            )
        else:
            row.digest = digest
            row.markdown = markdown
            row.payload = payload_of(report)
