"""
Strategy ABC.

Every strategy file under ``src/strategies/<type>/<market>/strategies/``
exports a single subclass of ``Strategy``. The bucket runner discovers
these subclasses dynamically via ``shared.strategy_loader``.

A strategy is intentionally a **pure decision-maker**: given a list of
scanner candidates and a market-data interface, it returns the subset it
wants to enter, plus an optional per-symbol entry hint for the sizer.

Sizing, order placement, and bookkeeping live OUTSIDE the strategy class.
This keeps strategies thin, testable, and importable by the backtester
(Decision 008, Decision 009).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar

from src.data_sources.base import MarketData


@dataclass(frozen=True, slots=True)
class EntryCandidate:
    """One symbol a strategy wants to enter.

    ``side`` is "buy" or "sell" — the order manager translates side based on
    the position the strategy intends to open. ``hint`` is a free-form
    payload the strategy can pass to the sizer / audit log (e.g. a signal
    score, a confidence value). The sizer does not have to read it.
    """

    symbol: str
    side: str = "buy"
    hint: dict[str, object] = field(default_factory=dict)


class Strategy(ABC):
    """Base class. Concrete strategies override ``select_entries``.

    Attributes:
        name: must equal the module's filename (validated against the
            Strategy Master CSV at bucket startup).
        tf: timeframe (e.g. "1d", "1h", "5m") — must match the bucket's
            regime/scanner TF.
        trading_type: one of "longterm", "swing", "scalp", "gambling".
    """

    name: ClassVar[str] = ""
    tf: ClassVar[str] = ""
    trading_type: ClassVar[str] = ""

    @abstractmethod
    def select_entries(
        self,
        candidates: list[str],
        data: MarketData,
    ) -> list[EntryCandidate]:
        """Return entry candidates from the scanner's filtered list.

        Pure function of candidates + market data + strategy state. Must NOT
        place orders, write to the DB, or call brokers. The runner orchestrates
        sizing and execution from this output.
        """

    # ---- Optional hooks (override if needed) -----------------------------

    def select_exits(
        self,
        held: dict[str, "Position"],  # noqa: F821 - forward ref to ORM type
        data: MarketData,
    ) -> list[str]:
        """Return symbols to close. Default: do nothing (held positions persist).

        Strategies that want explicit exits (mean-reversion, stop-loss) override.
        """
        return []

    def kelly_inputs(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """Per-symbol (μ, σ) used by the Kelly sizer.

        Default returns None → sizer falls back to ``allocator.yaml`` defaults.
        Override only if the strategy carries its own per-symbol stats.
        """
        return None
