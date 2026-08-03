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
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    BrokerName,
    OrderSide,
    OrderStatus,
    Position,
    PositionSide,
    SessionReport,
    SizingSnapshot,
    Trade,
)
from src.order_manager.ownership import bot_owned_quantities
from src.order_manager.pnl import pnl_pct, profit_factor, win_rate
from src.reporting.slippage import Slippage, decompose, mean_bps, to_decimal
from src.shared.allocator.sizer import BacktestBaseline, load_allocator_config
from src.shared.bucket import load_buckets
from src.shared.market_calendar import IST

_log = get_logger("reporting.eod")

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
    slip: Slippage = field(default_factory=Slippage)

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


# A live edge estimate below this many round-trips is noise, not evidence. Both
# buckets went live in July 2026, so every early report is under it — and must
# say so rather than let a 3-trade profit factor read as a verdict.
MIN_TRADES_FOR_SIGNAL = 20

# How far back the live-vs-backtest comparison looks.
EDGE_WINDOW_DAYS = 90


@dataclass(frozen=True, slots=True)
class EdgeStats:
    """Live edge for one bucket over the rolling window, vs its backtest."""

    bucket_id: str
    trades: int
    profit_factor: Decimal | None
    win_rate: Decimal | None
    mean_return: Decimal | None
    baseline: BacktestBaseline | None

    @property
    def significant(self) -> bool:
        return self.trades >= MIN_TRADES_FOR_SIGNAL


@dataclass(frozen=True, slots=True)
class ScanLine:
    """One scanner pass — proof the bot looked, and what it saw."""

    at: datetime
    strategy_id: str
    message: str


@dataclass(frozen=True, slots=True)
class Report:
    session_date: date_
    buckets: list[BucketSection]
    events: list[str]
    quiet: bool  # nothing traded, nothing tripped, nothing carried
    # Every SCANNER_RUN of the session. Without these a no-signal day renders
    # identically to a day the bot never woke up — and "the scanners ran and
    # found nothing" is the single most common true state of this system.
    scans: list[ScanLine] = field(default_factory=list)
    # Open positions on a shared account that the bot does NOT own — the
    # user's own trading (Decision 027). Reported so the numbers above are
    # visibly complete, never counted as the bot's.
    foreign: list[CarriedPosition] = field(default_factory=list)
    # Rolling live-vs-backtest edge per bucket (Decision 033).
    edge: list[EdgeStats] = field(default_factory=list)

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
    extra = trade.extra or {}
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
        # Decision 033. Trades placed before the signal-price change simply
        # report unknown rather than a misleading zero.
        slip=decompose(
            signal_price=extra.get("signal_price"),
            decision_price=extra.get("decision_price"),
            fill_price=extra.get("avg_fill_price"),
            side=trade.side,
        ),
    )


def split_positions(
    positions: list[Position],
    owned: dict[str, Decimal] | None = None,
    shared_broker: BrokerName = BrokerName.DHAN,
) -> tuple[list[Position], list[Position]]:
    """Split open positions into (the bot's, everyone else's). PURE.

    The 2026-07-22 near-miss was caused by reading the ACCOUNT and calling it
    the bot — the stop sweep tried to manage the user's NIFTY options. The
    scoping fix (Decision 027) taught the runtime paths to ask ownership
    first; a report that skipped the question would re-tell the same lie in
    prose, claiming the user's own positions as bot risk carried overnight.

    Two independent disqualifiers, either one is enough:

    * **No ``bucket_id``.** Decision 013 stamps one on every bot position, so a
      row without one was orphan-imported from the account, not opened here.
      (The live rows 244/245 are exactly this — adopted on 2026-07-22 before
      the scoping fix, and still non-flat.)
    * **Not in ``owned``** — applied ONLY to ``shared_broker`` positions. Every
      crypto bucket trades its own Delta sub-account (Decision 019), where the
      whole account is the bot's and the ledger check would wrongly disown a
      legitimate position (``owned`` is built from Dhan trades alone).
      ``owned=None`` disables the ledger check entirely.
    """
    bot: list[Position] = []
    foreign: list[Position] = []
    for pos in positions:
        attributed = bool(pos.bucket_id)
        shared = pos.broker == shared_broker
        in_ledger = (
            owned is None or not shared or owned.get(pos.symbol, _ZERO) > 0
        )
        (bot if attributed and in_ledger else foreign).append(pos)
    return bot, foreign


def to_carried(pos: Position) -> CarriedPosition:
    return CarriedPosition(
        bucket_id=pos.bucket_id or pos.strategy_id,
        symbol=pos.symbol,
        quantity=pos.quantity,
        entry_price=pos.entry_price,
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
        to_carried(p)
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


def build_edge(
    *,
    bucket_id: str,
    round_trips: list[tuple[Decimal, Decimal]],
    baseline: BacktestBaseline | None,
) -> EdgeStats:
    """Live edge from closed round-trips. PURE.

    ``round_trips`` is [(realized_pnl, notional)]. Profit factor and win rate
    are scale-invariant, so a live figure in rupees compares directly to a
    backtest figure computed on unlevered returns. ``mean_return`` normalises
    by notional to match the baseline's convention — measured on the EXIT leg's
    notional, which is the closest thing the ledger carries to the position's
    size and differs from the entry's only by the move itself.
    """
    pnls = [pnl for pnl, _ in round_trips]
    returns = [pnl / notional for pnl, notional in round_trips if notional > 0]
    return EdgeStats(
        bucket_id=bucket_id,
        trades=len(pnls),
        profit_factor=profit_factor(pnls),
        win_rate=win_rate(pnls),
        mean_return=(
            sum(returns, _ZERO) / Decimal(len(returns)) if returns else None
        ),
        baseline=baseline,
    )


def build_scans(rows: list[AuditLog]) -> list[ScanLine]:
    """SCANNER_RUN rows → display lines, oldest first. PURE."""
    return [
        ScanLine(
            at=r.ts,
            strategy_id=r.strategy_id or "—",
            message=r.message,
        )
        for r in rows
    ]


def build_events(rows: list[AuditLog]) -> list[str]:
    """One line per thing that tripped today. PURE.

    ``AuditLog`` timestamps its rows with ``ts``, not ``created_at`` — it is
    the one model that does not inherit ``TimestampMixin``. Rendered in IST
    because the whole report is about an IST session.
    """
    return [
        f"{r.ts.astimezone(IST):%H:%M} · {r.event_type.value} · {r.message}"
        for r in rows
    ]


def build_report(
    *,
    session_date: date_,
    trades: list[Trade],
    skips: list[SizingSnapshot],
    positions: list[Position],
    events: list[AuditLog],
    owned: dict[str, Decimal] | None = None,
    edge: list[EdgeStats] | None = None,
    scans: list[AuditLog] | None = None,
) -> Report:
    bot_positions, foreign_positions = split_positions(positions, owned)
    sections = build_sections(trades, skips, bot_positions)
    event_lines = build_events(events)
    # Scans deliberately do NOT clear `quiet` — a day the scanners ran and
    # found nothing IS a quiet day. They are evidence for that verdict, not
    # a contradiction of it.
    quiet = not event_lines and not any(
        s.traded or s.carried or s.skips for s in sections
    )
    return Report(
        session_date=session_date,
        buckets=sections,
        events=event_lines,
        quiet=quiet,
        foreign=[
            to_carried(p)
            for p in foreign_positions
            if p.side != PositionSide.FLAT and p.quantity > 0
        ],
        edge=edge or [],
        scans=build_scans(scans or []),
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
    foreign_note = (
        f"(+{len(report.foreign)} position(s) on the account not the bot's)"
        if report.foreign
        else None
    )
    scan_note = (
        f"Scanners ran {len(report.scans)} pass(es); nothing qualified."
        if report.scans
        else "NO SCANNER PASS RECORDED — check the bot is alive."
    )
    if report.quiet:
        # "Quiet" is a statement about the BOT, and it is only earned when the
        # scanners actually ran: a genuinely quiet day and a dead bot both
        # trade nothing. With no scan on record we report the facts and drop
        # the reassuring label rather than assert something unevidenced.
        verdict = (
            "No trades, nothing tripped — quiet day."
            if report.scans
            else "No trades, nothing tripped."
        )
        lines = [head, verdict, scan_note]
        if foreign_note:
            lines.append(foreign_note)
        return "\n".join(lines)

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
    out.append(scan_note)
    if foreign_note:
        out.append(foreign_note)
    if report.events:
        out.append(f"EVENTS ({len(report.events)}):")
        out.extend(f"  {line}" for line in report.events[:5])
        if len(report.events) > 5:
            out.append(f"  …and {len(report.events) - 5} more — see /journal")
    return "\n".join(out)


def _bps(value: Decimal | None) -> str:
    """bps, cost-positive. Em-dash when the price it needs was not recorded."""
    if value is None:
        return "—"
    return f"{value:+.1f}"


def _trade_table(lines: list[TradeLine]) -> list[str]:
    out = [
        "| time | symbol | strategy | qty | price | notional | fees | realized "
        "| lag bps | exec bps | slip bps |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in lines:
        when = f"{t.at.astimezone(IST):%H:%M}" if t.at else "—"
        out.append(
            f"| {when} | {t.symbol} | {t.strategy_name} | {t.quantity} | "
            f"{_money(t.price)} | {_money(t.notional)} | {_money(t.fees)} | "
            f"{_money(t.realized)} | {_bps(t.slip.lag_bps)} | "
            f"{_bps(t.slip.execution_bps)} | {_bps(t.slip.total_bps)} |"
        )
    return out


def _slippage_block(sec: BucketSection) -> list[str]:
    """Per-bucket slippage summary. Positive bps = cost."""
    lines = sec.entries + sec.exits
    lag = mean_bps([t.slip.lag_bps for t in lines])
    execution = mean_bps([t.slip.execution_bps for t in lines])
    total = mean_bps([t.slip.total_bps for t in lines])
    if lag is None and execution is None and total is None:
        return []
    return [
        "### Slippage",
        "",
        "Positive is cost, on both sides of the book. **Lag** is the market "
        "moving between the signal bar closing and the order going out — a "
        "latency problem. **Exec** is spread and impact on the market order — "
        "a liquidity or order-type problem. They have different fixes, which "
        "is why they are shown apart.",
        "",
        f"- decision lag: **{_bps(lag)} bps**",
        f"- execution: **{_bps(execution)} bps**",
        f"- total signal→fill: **{_bps(total)} bps**",
        "",
    ]


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _ratio(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _edge_block(report: Report) -> list[str]:
    """Live edge vs the backtest that justified running the strategy.

    The point of the whole exercise: a rupee P&L tells you what happened, not
    whether the thing still works.
    """
    stats = [e for e in report.edge if e.trades]
    if not stats:
        return []

    out = [
        "## Live vs backtest",
        "",
        f"Closed round-trips over the last {EDGE_WINDOW_DAYS} days. Profit "
        f"factor and win rate are scale-invariant, so the live figures compare "
        f"directly to backtest figures computed on unlevered returns.",
        "",
    ]

    thin = [e for e in stats if not e.significant]
    if thin:
        out += [
            f"> **Too early to read.** "
            f"{', '.join(e.bucket_id for e in thin)} "
            f"{'has' if len(thin) == 1 else 'have'} fewer than "
            f"{MIN_TRADES_FOR_SIGNAL} closed round-trips. At this sample size "
            f"a profit factor is noise — one trade moves it enormously. These "
            f"numbers are here to accumulate, not yet to judge.",
            "",
        ]

    out += [
        "| bucket | trades | PF live | PF backtest | win live | win backtest "
        "| mean live | mean backtest |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in stats:
        b = e.baseline
        out.append(
            f"| {e.bucket_id} | {e.trades} | {_ratio(e.profit_factor)} | "
            f"{_ratio(b.profit_factor if b else None)} | "
            f"{_pct(e.win_rate)} | {_pct(b.win_rate if b else None)} | "
            f"{_pct(e.mean_return)} | "
            f"{_pct(b.mean_trade_return if b else None)} |"
        )
    out.append("")

    if any(e.profit_factor is None and e.trades for e in stats):
        out += [
            "A blank live PF means no losing round-trip yet — undefined, not "
            "infinite.",
            "",
        ]
    return out


def _scan_block(report: Report) -> list[str]:
    """What the scanners looked at. Rendered on quiet days above all.

    A report that says only "quiet day" cannot be distinguished from a report
    written while the bot was dead. This section is the difference — it shows
    the passes that ran, what each evaluated, and that each found nothing.
    """
    if not report.scans:
        return []
    out = [
        "## What the scanners saw",
        "",
        f"{len(report.scans)} scanner pass(es) completed. Each line is the "
        f"universe the bot actually evaluated and what came back — the "
        f"evidence behind a no-signal day.",
        "",
        "| time | strategy | result |",
        "|---|---|---|",
    ]
    for s in report.scans:
        out.append(
            f"| {s.at.astimezone(IST):%H:%M} | {s.strategy_id} | {s.message} |"
        )
    out.append("")
    return out


def _foreign_block(report: Report) -> list[str]:
    """The "not the bot's" section. Rendered on quiet days too — a day the bot
    sat out is still a day the account held something."""
    if not report.foreign:
        return []
    total = sum((c.notional for c in report.foreign), _ZERO)
    out = [
        "## Not the bot's",
        "",
        f"{len(report.foreign)} open position(s) on the shared Dhan account, "
        f"{_money(total)} at entry, that the bot did not open (Decision 027). "
        f"Listed so the numbers above are visibly complete — they are "
        f"**excluded** from every P&L and carried figure in this report, and "
        f"no safety path acts on them.",
        "",
        "| symbol | qty | entry | notional |",
        "|---|---|---|---|",
    ]
    for c in report.foreign:
        out.append(
            f"| {c.symbol} | {c.quantity} | {_money(c.entry_price)} | "
            f"{_money(c.notional)} |"
        )
    out.append("")
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
        # The rolling edge still matters on a day the bot sat out — it is a
        # 90-day view, not a today view. The scan block matters MORE here than
        # anywhere else: on a quiet day it is the only proof the bot looked.
        return "\n".join(
            out + _scan_block(report) + _edge_block(report) + _foreign_block(report)
        )

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

        out += _slippage_block(sec)

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

    out += _scan_block(report)
    out += _edge_block(report)
    out += _foreign_block(report)

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
    # Decision 033's process watchdog. The module docstring above has always
    # claimed this report answers "did anything trip — invariants, breakers,
    # kill switch, rejects?"; until invariants wrote an audit row (and until
    # this line existed) the invariant half of that sentence was false, and a
    # violation that cleared before halting anything read as a quiet day.
    AuditEventType.INVARIANT_VIOLATED,
    AuditEventType.REGIME_CHANGE,
    AuditEventType.DRIFT_ALERT,
    AuditEventType.RECONCILE_DIFF,
)


def load_baselines() -> dict[str, BacktestBaseline]:
    """``{bucket_id: baseline}`` from each bucket's allocator.yaml.

    Best-effort: a bucket whose config fails to load simply reports no
    baseline. The EOD report must never be the thing that surfaces a YAML
    error as a crash — the trading path already fails fast on those.
    """
    out: dict[str, BacktestBaseline] = {}
    try:
        buckets = load_buckets()
    except Exception:
        _log.warning("edge_baseline_bucket_load_failed", exc_info=True)
        return out
    for bucket in buckets:
        try:
            cfg = load_allocator_config(bucket.allocator_yaml_path)
        except Exception:
            _log.warning(
                "edge_baseline_allocator_load_failed",
                bucket_id=bucket.id,
                exc_info=True,
            )
            continue
        if cfg.backtest_baseline is not None:
            out[bucket.id] = cfg.backtest_baseline
    return out


def round_trips_by_bucket(
    session, since: datetime
) -> dict[str, list[tuple[Decimal, Decimal]]]:
    """``{bucket_id: [(realized_pnl, exit_notional)]}`` for closed round-trips.

    A round-trip is an exit the reconciler stamped ``realized_pnl`` on. Its
    notional is taken from the exit's own fill (qty × avg_fill_price), falling
    back to the recorded price.
    """
    rows = (
        session.execute(
            select(Trade).where(
                Trade.created_at >= since,
                Trade.status.in_([OrderStatus.FILLED, OrderStatus.PARTIAL]),
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for trade in rows:
        pnl = _realized_of(trade)
        if pnl is None:
            continue
        extra = trade.extra or {}
        price = to_decimal(extra.get("avg_fill_price")) or trade.price
        notional = (trade.quantity or _ZERO) * (price or _ZERO)
        bucket_id = trade.bucket_id or trade.strategy_id
        out.setdefault(bucket_id, []).append((pnl, notional))
    return out


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
                    AuditLog.ts >= start,
                    AuditLog.ts < end,
                    AuditLog.event_type.in_(_REPORTABLE_EVENTS),
                )
                .order_by(AuditLog.ts)
            ).scalars()
        )
        # Kept separate from `events`: a scanner pass is not something that
        # went wrong, it is the record of routine work being done.
        scans = list(
            session.execute(
                select(AuditLog)
                .where(
                    AuditLog.ts >= start,
                    AuditLog.ts < end,
                    AuditLog.event_type == AuditEventType.SCANNER_RUN,
                )
                .order_by(AuditLog.ts)
            ).scalars()
        )
        # Ownership for the SHARED Dhan account (Decision 027). Derived from
        # the bot's own Trade rows, never from account state — the same source
        # the stop sweep and reconciler use, so the report cannot disagree
        # with what the safety paths believe.
        dhan_buckets = sorted(
            {
                p.bucket_id
                for p in positions
                if p.broker == BrokerName.DHAN and p.bucket_id
            }
            | {
                t.bucket_id
                for t in trades
                if t.broker == BrokerName.DHAN and t.bucket_id
            }
        )
        owned = bot_owned_quantities(
            session,
            broker_name=BrokerName.DHAN,
            bucket_ids=dhan_buckets,
            now=end,
        )
        # Rolling live-vs-backtest edge (Decision 033) — a wider window than
        # the rest of the report, because one session is never enough trades
        # to say anything about an edge.
        baselines = load_baselines()
        trips = round_trips_by_bucket(
            session, end - timedelta(days=EDGE_WINDOW_DAYS)
        )
        edge = [
            build_edge(
                bucket_id=bucket_id,
                round_trips=rt,
                baseline=baselines.get(bucket_id),
            )
            for bucket_id, rt in sorted(trips.items())
        ]

        return build_report(
            session_date=session_date,
            trades=trades,
            skips=skips,
            positions=positions,
            events=events,
            owned=owned,
            edge=edge,
            scans=scans,
        )


def _opt_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def payload_of(report: Report) -> dict:
    """The structured numbers behind the prose, for later analysis."""
    return {
        "realized": str(report.realized),
        "fees": str(report.fees),
        "quiet": report.quiet,
        "events": len(report.events),
        "scans": len(report.scans),
        "buckets": {
            s.bucket_id: {
                "entries": len(s.entries),
                "exits": len(s.exits),
                "rejects": len(s.rejects),
                "carried": len(s.carried),
                "realized": str(s.realized),
                "fees": str(s.fees),
                "skip_reasons": dict(Counter(x.decision for x in s.skips)),
                "slippage_bps": {
                    "lag": _opt_str(
                        mean_bps([t.slip.lag_bps for t in s.entries + s.exits])
                    ),
                    "execution": _opt_str(
                        mean_bps(
                            [t.slip.execution_bps for t in s.entries + s.exits]
                        )
                    ),
                    "total": _opt_str(
                        mean_bps([t.slip.total_bps for t in s.entries + s.exits])
                    ),
                },
            }
            for s in report.buckets
        },
        "edge": {
            e.bucket_id: {
                "trades": e.trades,
                "profit_factor": _opt_str(e.profit_factor),
                "win_rate": _opt_str(e.win_rate),
                "mean_return": _opt_str(e.mean_return),
                "significant": e.significant,
            }
            for e in report.edge
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
