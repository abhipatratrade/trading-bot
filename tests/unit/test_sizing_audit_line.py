"""The SIZING_DECISION line names WHY — Phase 11b.

BLUESTARCO, 2026-08-07: the scanner logged `1/1 crossed of 94 evaluated`, the
sizer logged `sized 1 candidates ... 0 placed` every tick for the rest of the
session, and no order was ever attempted. The reason existed — `sizing_snapshot`
held "missing or non-positive mark price" 38 times — but the audit log, which is
what a person reads, carried only the enum. The August reconciliation reported
it as "no reason was recorded"; that it took code archaeology to disprove is the
same defect.
"""

from __future__ import annotations

from src.core.models import SizingDecision
from src.shared.allocator.sizer import SizingResult, sizing_audit_line


def _r(sym: str, decision: SizingDecision, reason: str = "") -> SizingResult:
    return SizingResult(symbol=sym, decision=decision, reason=reason or None)


def test_the_august_line_now_carries_its_cause():
    msg, skipped = sizing_audit_line(
        {
            "BLUESTARCO": _r(
                "BLUESTARCO",
                SizingDecision.SKIPPED_OTHER,
                "missing or non-positive mark price",
            )
        },
        strategy_name="mean_reversion_1h",
        bucket_id="swing-indian",
    )
    assert msg == (
        "sized 1 candidates for mean_reversion_1h@swing-indian: 0 placed "
        "(BLUESTARCO: missing or non-positive mark price)"
    )
    assert skipped == {"BLUESTARCO": "missing or non-positive mark price"}


def test_all_placed_leaves_the_line_and_payload_unchanged():
    """The happy path must not grow noise — or a `skipped` key."""
    msg, skipped = sizing_audit_line(
        {"PIIND": _r("PIIND", SizingDecision.PLACED)},
        strategy_name="mean_reversion_1h",
        bucket_id="swing-indian",
    )
    assert msg == "sized 1 candidates for mean_reversion_1h@swing-indian: 1 placed"
    assert skipped == {}


def test_several_skips_name_one_and_count_the_rest():
    msg, skipped = sizing_audit_line(
        {
            "IREDA": _r("IREDA", SizingDecision.SKIPPED_INSUFFICIENT, "margin"),
            "YESBANK": _r("YESBANK", SizingDecision.SKIPPED_DEDUP, "already open"),
            "PIIND": _r("PIIND", SizingDecision.PLACED),
        },
        strategy_name="mean_reversion_1h",
        bucket_id="swing-indian",
    )
    assert msg.endswith("1 placed (IREDA: margin, +1 more)")
    assert len(skipped) == 2


def test_a_reasonless_skip_falls_back_to_the_decision():
    """`skipped_other` is a poor answer, but it beats an empty one."""
    msg, skipped = sizing_audit_line(
        {"X": _r("X", SizingDecision.SKIPPED_OTHER)},
        strategy_name="s",
        bucket_id="b",
    )
    assert msg.endswith("(X: skipped_other)")
    assert skipped == {"X": "skipped_other"}


def test_the_named_skip_is_the_first_candidate_not_an_arbitrary_one():
    """Order is chronological — dict insertion follows candidate order."""
    msg, _ = sizing_audit_line(
        {
            "FIRST": _r("FIRST", SizingDecision.SKIPPED_NEGATIVE_EDGE, "mu<=0"),
            "SECOND": _r("SECOND", SizingDecision.SKIPPED_INSUFFICIENT, "margin"),
        },
        strategy_name="s",
        bucket_id="b",
    )
    assert "FIRST: mu<=0" in msg
    assert "SECOND" not in msg


def test_no_candidates_is_still_a_clean_line():
    msg, skipped = sizing_audit_line({}, strategy_name="s", bucket_id="b")
    assert msg == "sized 0 candidates for s@b: 0 placed"
    assert skipped == {}
