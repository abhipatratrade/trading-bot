"""
Gap-down reversal over the BROAD universe (Midcap 150 + Smallcap 100).

Identical signal logic to ``gap_down_reversal`` — it is the same strategy
pointed at a different scanner set (``scanner_broad.yaml`` +
``allocator_broad.yaml``, Decision 026). It exists as a separate class purely
because ``strategy_master.csv`` keys on strategy_name and rejects duplicates,
so one strategy cannot appear twice with two scanner sets.

⚠️  NOT BACKTEST-VALIDATED. The frozen PF 1.68 holdout covers NIFTY-100 only.
This set runs to produce its own evidence; judge it on its own P&L, which is
attributable separately because it trades under its own strategy_name.

Keeping it a subclass is deliberate: any fix to the entry/exit logic lands in
both sets automatically, so the two can never silently diverge.
"""

from __future__ import annotations

from typing import ClassVar

from src.strategies.intraday.indian.strategies.gap_down_reversal import (
    GapDownReversal,
)


class GapDownReversalBroad(GapDownReversal):
    """Same fade, broader (unvalidated) universe."""

    name: ClassVar[str] = "gap_down_reversal_broad"
