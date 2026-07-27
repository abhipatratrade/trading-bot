"""EOD session report — pure builders and renderers, no DB."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.core.models import (
    AuditEventType,
    BrokerName,
    OrderSide,
    OrderStatus,
    PositionSide,
    SizingDecision,
)
from src.dashboard.routes.journal import render_markdown_to_html
from src.reporting.eod import (
    build_report,
    build_sections,
    ist_day_bounds,
    payload_of,
    render_digest,
    render_markdown,
)
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


class _Audit:
    def __init__(self, message: str = "Kill switch ENGAGED") -> None:
        self.event_type = AuditEventType.KILL_SWITCH_FLIPPED
        self.message = message
        self.created_at = datetime(2026, 7, 28, 15, 20, tzinfo=IST)


def _report(**kw) -> object:
    return build_report(
        session_date=DAY,
        trades=kw.get("trades", []),
        skips=kw.get("skips", []),
        positions=kw.get("positions", []),
        events=kw.get("events", []),
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
    text = render_digest(_report())
    assert "Quiet day" in text
    assert "Realized" not in text


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
