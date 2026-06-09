"""
top5_volume — Phase 1 strategy ported into the bucket framework.

The scanner already picks the top-5 perps by 24h volume on Delta India
(per ``scanner.yaml``). This strategy's job is the trivial one of
"trade everything in the scanner's universe".

That makes the strategy a one-liner — but the bucket-pipeline glue
(Strategy Master gate, dedup gate, Kelly sizing, regime multiplier)
still applies via ``BucketRunner`` and ``shared.allocator.sizer``.
The equal-weight ×5x leverage logic that lived in the legacy runner's
``_calculate_sizes`` is now sized by Kelly against bucket capital
(Decision 015).
"""

from __future__ import annotations

from src.data_sources.base import MarketData
from src.shared.base_strategy import EntryCandidate, Strategy


class Top5VolumeLongterm(Strategy):
    """Phase 1: long-only entries on every symbol the scanner picked."""

    name = "top5_volume"
    tf = "1d"
    trading_type = "longterm"

    def select_entries(
        self,
        candidates: list[str],
        data: MarketData,  # noqa: ARG002 — interface signature
    ) -> list[EntryCandidate]:
        return [
            EntryCandidate(symbol=sym, side="buy", hint={"source": "volume_rank"})
            for sym in candidates
        ]
