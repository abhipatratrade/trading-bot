"""EOD session report — pure builders and renderers, no DB."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect as sa_inspect

from src.core.models import (
    AuditEventType,
    AuditLog,
    BrokerName,
    OrderSide,
    OrderStatus,
    Position,
    PositionSide,
    SizingDecision,
    SizingSnapshot,
    Trade,
)
from src.dashboard.routes.journal import render_markdown_to_html
from src.reporting.eod import (
    EdgeStats,
    build_edge,
    build_report,
    build_sections,
    ist_day_bounds,
    load_baselines,
    payload_of,
    render_digest,
    render_markdown,
    split_positions,
    to_trade_line,
)
from src.shared.allocator.sizer import BacktestBaseline
from src.shared.market_calendar import IST

DAY = date(2026, 7, 28)


class _Trade:
    """Duck-types the Trade columns the report reads (models need Postgres)."""

    def __init__(
        self,
        symbol: str = "TCS",
        side: OrderSide = OrderSide.BUY,
        qty: str = "10",
        price: str = "100",
        status: OrderStatus = OrderStatus.FILLED,
        fees: str = "20",
        realized: str | None = None,
        bucket_id: str = "swing-indian",
    ) -> None:
        self.symbol = symbol
        self.side = side
        self.quantity = Decimal(qty)
        self.price = Decimal(price)
        self.status = status
        self.fees = Decimal(fees)
        self.bucket_id = bucket_id
        self.strategy_id = bucket_id
        self.strategy_name = "mean_reversion_1h"
        self.broker = BrokerName.DHAN
        self.extra = {"realized_pnl": realized} if realized is not None else None
        self.filled_at = datetime(2026, 7, 28, 10, 30, tzinfo=IST)
        self.submitted_at = self.filled_at
        self.created_at = self.filled_at


class _Skip:
    def __init__(
        self,
        symbol: str = "INFY",
        decision: SizingDecision = SizingDecision.SKIPPED_INSUFFICIENT,
        bucket_id: str = "swing-indian",
    ) -> None:
        self.symbol = symbol
        self.decision = decision
        self.bucket_id = bucket_id
        self.strategy_name = "mean_reversion_1h"
        self.reason = "not enough available balance"


class _Position:
    def __init__(
        self,
        symbol: str = "TCS",
        qty: str = "10",
        entry: str = "100",
        side: PositionSide = PositionSide.LONG,
        bucket_id: str = "swing-indian",
    ) -> None:
        self.symbol = symbol
        self.quantity = Decimal(qty)
        self.entry_price = Decimal(entry)
        self.side = side
        self.bucket_id = bucket_id
        self.strategy_id = bucket_id
        self.broker = BrokerName.DHAN


class _Scan:
    def __init__(
        self,
        message: str = "gap-reversal scan: 0/99 gapped down, top-5",
        strategy_id: str = "intraday-indian",
    ) -> None:
        self.event_type = AuditEventType.SCANNER_RUN
        self.strategy_id = strategy_id
        self.message = message
        self.ts = datetime(2026, 7, 28, 9, 31, tzinfo=IST)


class _Audit:
    def __init__(self, message: str = "Kill switch ENGAGED") -> None:
        self.event_type = AuditEventType.KILL_SWITCH_FLIPPED
        self.strategy_id = None
        self.message = message
        # `ts`, not `created_at` — AuditLog is the one model that does not
        # inherit TimestampMixin. test_fakes_match_the_real_orm_columns keeps
        # this fake honest.
        self.ts = datetime(2026, 7, 28, 15, 20, tzinfo=IST)


@pytest.mark.parametrize(
    ("fake", "model"),
    [
        (_Trade(), Trade),
        (_Skip(), SizingSnapshot),
        (_Position(), Position),
        (_Audit(), AuditLog),
        (_Scan(), AuditLog),
    ],
)
def test_fakes_match_the_real_orm_columns(fake: object, model: type) -> None:
    """Every attribute the fakes expose must exist on the real model.

    These tests duck-type the ORM so they need no database, which means a fake
    written to match a WRONG assumption passes happily. That is exactly what
    happened: the report read ``AuditLog.created_at``, the fake obligingly had
    one, and the real table has ``ts`` — AuditLog is the single model that
    does not inherit TimestampMixin. It would have crashed at 15:45 on the
    first day an event was logged. This is the check that catches it.
    """
    mapped = {attr.key for attr in sa_inspect(model).attrs}
    missing = {k for k in vars(fake) if not k.startswith("_")} - mapped
    assert not missing, f"{model.__name__} has no column(s): {sorted(missing)}"


def _report(**kw) -> object:
    return build_report(
        session_date=DAY,
        trades=kw.get("trades", []),
        skips=kw.get("skips", []),
        positions=kw.get("positions", []),
        events=kw.get("events", []),
        owned=kw.get("owned"),
        edge=kw.get("edge"),
        scans=kw.get("scans"),
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def test_buys_are_entries_and_sells_are_exits() -> None:
    sections = build_sections(
        [_Trade(side=OrderSide.BUY), _Trade(side=OrderSide.SELL, realized="500")],
        [],
        [],
    )
    assert len(sections) == 1
    assert len(sections[0].entries) == 1
    assert len(sections[0].exits) == 1
    assert sections[0].realized == Decimal("500")


def test_rejected_orders_are_split_out_of_entries() -> None:
    sections = build_sections(
        [_Trade(status=OrderStatus.REJECTED), _Trade(status=OrderStatus.FILLED)],
        [],
        [],
    )
    assert len(sections[0].rejects) == 1
    assert len(sections[0].entries) == 1


def test_fees_exclude_rejected_orders() -> None:
    """A rejected order never executed, so its fee row is not a real cost."""
    sections = build_sections(
        [_Trade(status=OrderStatus.REJECTED, fees="20"), _Trade(fees="20")], [], []
    )
    assert sections[0].fees == Decimal("20")


def test_sections_are_one_per_bucket_and_sorted() -> None:
    sections = build_sections(
        [_Trade(bucket_id="swing-indian"), _Trade(bucket_id="intraday-indian")], [], []
    )
    assert [s.bucket_id for s in sections] == ["intraday-indian", "swing-indian"]


def test_a_bucket_that_only_declined_signals_still_appears() -> None:
    """"Why didn't it trade?" is exactly the question the report exists for."""
    sections = build_sections([], [_Skip()], [])
    assert len(sections) == 1
    assert not sections[0].traded
    assert len(sections[0].skips) == 1


def test_flat_positions_are_not_carried() -> None:
    sections = build_sections([], [], [_Position(side=PositionSide.FLAT)])
    assert sections == []


# ---------------------------------------------------------------------------
# Ownership split — the 2026-07-22 lesson, applied to reporting
# ---------------------------------------------------------------------------
def test_an_unattributed_position_is_not_the_bots() -> None:
    """Live rows 244/245: orphan-imported before the scoping fix.

    No bucket_id, so Decision 013 says the bot never opened it. Reporting the
    user's NIFTY options as bot risk carried overnight would re-tell the
    2026-07-22 lie in prose.
    """
    orphan = _Position(symbol="NIFTY-Jul2026-24450-CE")
    orphan.bucket_id = None
    bot, foreign = split_positions([orphan], owned={})
    assert bot == []
    assert [p.symbol for p in foreign] == ["NIFTY-Jul2026-24450-CE"]


def test_an_attributed_position_absent_from_the_ledger_is_not_the_bots() -> None:
    bot, foreign = split_positions([_Position(symbol="TCS")], owned={})
    assert bot == []
    assert len(foreign) == 1


def test_an_attributed_and_owned_position_is_the_bots() -> None:
    bot, foreign = split_positions(
        [_Position(symbol="TCS")], owned={"TCS": Decimal("10")}
    )
    assert len(bot) == 1
    assert foreign == []


def test_the_ledger_check_is_scoped_to_the_shared_broker() -> None:
    """Crypto sub-accounts are exclusively the bot's (Decision 019).

    ``owned`` is built from Dhan trades alone, so applying it to a Delta
    position would disown a perfectly legitimate one.
    """
    delta = _Position(symbol="BTCUSD", bucket_id="longterm-crypto")
    delta.broker = BrokerName.DELTA_INDIA
    bot, foreign = split_positions([delta], owned={})
    assert len(bot) == 1
    assert foreign == []


def test_foreign_positions_are_excluded_from_carried_but_still_reported() -> None:
    orphan = _Position(symbol="NIFTY-Jul2026-24450-CE")
    orphan.bucket_id = None
    report = _report(positions=[orphan, _Position(symbol="TCS")], owned={})
    assert all(not s.carried for s in report.buckets)
    assert [c.symbol for c in report.foreign] == [
        "NIFTY-Jul2026-24450-CE",
        "TCS",
    ]


def test_markdown_labels_foreign_positions_as_not_the_bots() -> None:
    orphan = _Position(symbol="NIFTY-Jul2026-24450-CE")
    orphan.bucket_id = None
    text = render_markdown(_report(positions=[orphan], owned={}))
    assert "## Not the bot's" in text
    assert "excluded" in text
    assert "### Carried overnight" not in text


def test_digest_does_not_count_foreign_positions_as_overnight_risk() -> None:
    orphan = _Position(symbol="NIFTY-Jul2026-24450-CE")
    orphan.bucket_id = None
    text = render_digest(_report(positions=[orphan], owned={}))
    assert "OVERNIGHT" not in text
    assert "not the bot's" in text


def test_realized_ignores_a_missing_or_unparseable_extra() -> None:
    trade = _Trade(side=OrderSide.SELL)
    trade.extra = {"realized_pnl": "not-a-number"}
    sections = build_sections([trade, _Trade(side=OrderSide.SELL)], [], [])
    assert sections[0].realized == Decimal("0")


# ---------------------------------------------------------------------------
# Quiet day
# ---------------------------------------------------------------------------
def test_a_day_with_nothing_at_all_is_quiet() -> None:
    assert _report().quiet


def test_a_day_with_only_declined_signals_is_not_quiet() -> None:
    assert not _report(skips=[_Skip()]).quiet


def test_a_day_with_only_an_event_is_not_quiet() -> None:
    assert not _report(events=[_Audit()]).quiet


def test_quiet_digest_says_so_plainly() -> None:
    text = render_digest(_report(scans=[_Scan()]))
    assert "quiet day" in text
    assert "Realized" not in text


def test_quiet_label_is_withheld_when_no_scan_ran() -> None:
    """"Quiet day" is a claim about the bot working. Don't make it unevidenced."""
    text = render_digest(_report())
    assert "quiet day" not in text
    assert "NO SCANNER PASS RECORDED" in text


def test_quiet_markdown_explains_rather_than_looking_broken() -> None:
    text = render_markdown(_report())
    assert "quiet day is a real outcome" in text


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------
def test_digest_reports_totals_and_per_bucket_lines() -> None:
    text = render_digest(
        _report(trades=[_Trade(side=OrderSide.SELL, realized="500"), _Trade()])
    )
    assert "+500.00" in text
    assert "swing-indian" in text


def test_digest_flags_overnight_positions() -> None:
    text = render_digest(_report(positions=[_Position()]))
    assert "OVERNIGHT" in text
    assert "TCS" in text


def test_digest_truncates_a_long_event_list() -> None:
    text = render_digest(_report(events=[_Audit(f"e{i}") for i in range(9)]))
    assert "and 4 more" in text


def test_digest_marks_rejects_loudly() -> None:
    text = render_digest(_report(trades=[_Trade(status=OrderStatus.REJECTED)]))
    assert "REJECTED" in text


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def test_markdown_has_the_expected_sections() -> None:
    text = render_markdown(
        _report(
            trades=[_Trade(), _Trade(side=OrderSide.SELL, realized="500")],
            skips=[_Skip()],
            positions=[_Position()],
            events=[_Audit()],
        )
    )
    for heading in (
        "## swing-indian",
        "### Entries",
        "### Exits",
        "### Signals seen but not taken",
        "### Carried overnight",
        "## Events",
    ):
        assert heading in text, heading


def test_markdown_groups_skips_by_reason_with_counts() -> None:
    skips = [_Skip(symbol=f"S{i}") for i in range(12)]
    text = render_markdown(_report(skips=skips))
    assert "`skipped_insufficient` | 12" in text
    assert "+2" in text  # symbol list truncated at 10


def test_payload_carries_the_numbers_behind_the_prose() -> None:
    payload = payload_of(_report(trades=[_Trade()], skips=[_Skip()]))
    bucket = payload["buckets"]["swing-indian"]
    assert bucket["entries"] == 1
    assert bucket["skip_reasons"]["skipped_insufficient"] == 1


# ---------------------------------------------------------------------------
# Slippage in the report
# ---------------------------------------------------------------------------
def test_trade_line_carries_the_decomposed_slippage() -> None:
    t = _Trade()
    t.extra = {
        "signal_price": "100",
        "decision_price": "100.5",
        "avg_fill_price": "100.8",
    }
    line = to_trade_line(t)
    assert line.slip.lag_bps == Decimal("50")
    assert line.slip.total_bps == Decimal("80")


def test_a_pre_change_trade_reports_unknown_slippage_not_zero() -> None:
    """Trades placed before signal prices were recorded must not read as 0bps."""
    line = to_trade_line(_Trade())
    assert not line.slip.known


def test_markdown_shows_the_slippage_breakdown() -> None:
    t = _Trade()
    t.extra = {
        "signal_price": "100",
        "decision_price": "100.5",
        "avg_fill_price": "100.8",
    }
    text = render_markdown(_report(trades=[t]))
    assert "### Slippage" in text
    assert "decision lag" in text
    assert "execution" in text


def test_slippage_block_is_omitted_when_nothing_is_known() -> None:
    text = render_markdown(_report(trades=[_Trade()]))
    assert "### Slippage" not in text


# ---------------------------------------------------------------------------
# Live vs backtest
# ---------------------------------------------------------------------------
def _edge(trades: int, pf_pnls: list[str] | None = None) -> EdgeStats:
    pnls = pf_pnls or (["100"] * (trades - 1) + ["-50"] if trades else [])
    return build_edge(
        bucket_id="swing-indian",
        round_trips=[(Decimal(p), Decimal("10000")) for p in pnls],
        baseline=BacktestBaseline(profit_factor=Decimal("2.31"), trades=214),
    )


def test_edge_computes_live_pf_and_win_rate() -> None:
    e = build_edge(
        bucket_id="swing-indian",
        round_trips=[(Decimal("200"), Decimal("10000")), (Decimal("-100"), Decimal("10000"))],
        baseline=None,
    )
    assert e.profit_factor == Decimal("2")
    assert e.win_rate == Decimal("0.5")
    assert e.mean_return == Decimal("0.005")


def test_a_thin_sample_is_flagged_as_too_early_to_read() -> None:
    """Both buckets went live in July 2026 — every early report hits this."""
    e = _edge(3)
    assert not e.significant
    text = render_markdown(_report(edge=[e]))
    assert "Too early to read" in text
    assert "2.31" in text  # the baseline is still shown alongside


def test_a_sufficient_sample_is_not_flagged() -> None:
    e = _edge(25)
    assert e.significant
    text = render_markdown(_report(edge=[e]))
    assert "Too early to read" not in text


def test_an_undefined_live_pf_is_explained_not_printed_as_infinity() -> None:
    e = build_edge(
        bucket_id="swing-indian",
        round_trips=[(Decimal("100"), Decimal("10000"))],
        baseline=None,
    )
    assert e.profit_factor is None
    text = render_markdown(_report(edge=[e]))
    assert "undefined, not" in text


def test_edge_section_is_omitted_with_no_closed_round_trips() -> None:
    text = render_markdown(_report(edge=[_edge(0)]))
    assert "## Live vs backtest" not in text


def test_the_edge_section_survives_a_quiet_day() -> None:
    """It is a 90-day view, so a day the bot sat out does not erase it."""
    report = _report(edge=[_edge(25)])
    assert report.quiet
    assert "## Live vs backtest" in render_markdown(report)


def test_payload_carries_edge_and_slippage() -> None:
    t = _Trade()
    t.extra = {"signal_price": "100", "avg_fill_price": "101"}
    payload = payload_of(_report(trades=[t], edge=[_edge(3)]))
    total = payload["buckets"]["swing-indian"]["slippage_bps"]["total"]
    assert Decimal(total) == Decimal("100")  # str keeps Decimal scale ("100.00")
    assert payload["edge"]["swing-indian"]["significant"] is False


# ---------------------------------------------------------------------------
# The real allocator.yaml files must actually carry baselines
# ---------------------------------------------------------------------------
def test_both_live_buckets_declare_a_backtest_baseline() -> None:
    """A baseline that fails to load makes the comparison silently blank.

    Values are the fold each strategy's own docs nominate as the planning
    grade — intraday's HOLDOUT, swing's TRAIN — recomputed from the
    backtest_ref JSONs on 2026-08-01.
    """
    baselines = load_baselines()
    for bucket_id, expected_pf in (
        ("swing-indian", Decimal("2.313")),
        ("intraday-indian", Decimal("1.684")),
    ):
        assert bucket_id in baselines, bucket_id
        assert baselines[bucket_id].profit_factor == expected_pf


def test_every_baseline_is_complete_and_internally_consistent() -> None:
    """Guards the bug this replaced: PF from one fold, trades from another.

    A baseline mixing folds silently benchmarks live results against a
    composite that never existed. Completeness is the only cheap proxy — a
    fold supplies all four numbers or it wasn't really read.
    """
    for bucket_id, b in load_baselines().items():
        assert b.profit_factor is not None, bucket_id
        assert b.win_rate is not None, bucket_id
        assert b.mean_trade_return is not None, bucket_id
        assert b.trades, bucket_id
        assert 0 < b.win_rate < 1, f"{bucket_id} win_rate must be a fraction"


# ---------------------------------------------------------------------------
# Scanner passes — telling "looked and found nothing" from "never looked"
# ---------------------------------------------------------------------------
def test_scans_do_not_make_a_day_non_quiet() -> None:
    """Scanners running and finding nothing IS a quiet day, not a busy one."""
    report = _report(scans=[_Scan()])
    assert report.quiet


def test_a_quiet_day_still_shows_what_the_scanners_saw() -> None:
    """The whole point: proof the bot looked, on the day it did nothing.

    Without this, a genuinely quiet session and a bot that never woke up
    render as the same report.
    """
    text = render_markdown(_report(scans=[_Scan(), _Scan(strategy_id="swing-indian")]))
    assert "## What the scanners saw" in text
    assert "0/99 gapped down" in text
    assert "swing-indian" in text


def test_digest_reports_the_scan_count_on_a_quiet_day() -> None:
    text = render_digest(_report(scans=[_Scan(), _Scan()]))
    assert "Scanners ran 2 pass(es)" in text


def test_digest_shouts_when_no_scanner_pass_was_recorded() -> None:
    """Zero scans on a trading day means the bot didn't look — say so loudly."""
    text = render_digest(_report())
    assert "NO SCANNER PASS RECORDED" in text


def test_scan_block_is_omitted_when_there_are_no_scans() -> None:
    assert "## What the scanners saw" not in render_markdown(_report())


def test_scans_render_alongside_a_busy_day() -> None:
    text = render_markdown(_report(trades=[_Trade()], scans=[_Scan()]))
    assert "## What the scanners saw" in text
    assert "### Entries" in text


def test_payload_counts_scans() -> None:
    assert payload_of(_report(scans=[_Scan(), _Scan()]))["scans"] == 2


# ---------------------------------------------------------------------------
# Day bounds
# ---------------------------------------------------------------------------
def test_day_bounds_span_one_ist_day() -> None:
    start, end = ist_day_bounds(DAY)
    assert start.tzinfo is not None
    assert (end - start).total_seconds() == 86400
    assert start.astimezone(IST).hour == 0


# ---------------------------------------------------------------------------
# Markdown → HTML (dashboard)
# ---------------------------------------------------------------------------
def test_html_renders_headings_tables_and_lists() -> None:
    html = render_markdown_to_html(
        "# Title\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    assert "<h1>Title</h1>" in html
    assert "<li>one</li>" in html
    assert "<th>a</th>" in html
    assert "<td>1</td>" in html


def test_html_escapes_before_it_formats() -> None:
    """A broker message with angle brackets must not become markup."""
    html = render_markdown_to_html("- got <script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_renders_the_inline_subset() -> None:
    html = render_markdown_to_html("**bold** and `code`")
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html


def test_html_round_trips_a_real_report() -> None:
    markdown = render_markdown(
        _report(trades=[_Trade()], skips=[_Skip()], positions=[_Position()])
    )
    html = render_markdown_to_html(markdown)
    assert "<table>" in html
    assert "swing-indian" in html
