"""
Strategy Master CSV schema.

Per PPTX slide 4(c), each bucket has a Strategy Master sheet listing every
strategy with its TF, min vol, allowed regimes, and trading type. The
sheet is the single source of truth for "is this strategy eligible to
trade right now?".

Format: CSV in git (Decision 016), one file per bucket folder.

Columns:
    strategy_name       — matches filename in strategies/ (no .py)
    tf                  — must match bucket's regime tf
    min_vol             — 24h notional in bucket's quote currency (blank ⇒ no floor)
    trading_regime_1    — ∈ {bull, neutral, bear, ""}
    trading_regime_2    — ∈ {bull, neutral, bear, ""}
    trading_type        — must equal bucket.trading_type

Blank field ⇒ that constraint is absent. If BOTH regime slots are blank
the strategy is regime-agnostic. If exactly one is filled the strategy
trades only in that regime.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from src.core.models import MarketRegime


class StrategyMasterRow(BaseModel):
    """One row from a bucket's strategy_master.csv."""

    strategy_name: str = Field(min_length=1, max_length=64)
    tf: str = Field(min_length=1)
    min_vol: Decimal | None = None
    trading_regime_1: MarketRegime | None = None
    trading_regime_2: MarketRegime | None = None
    trading_type: str = Field(min_length=1)

    # CSV cells arrive as strings. Blank → None.
    @field_validator(
        "min_vol", "trading_regime_1", "trading_regime_2", mode="before"
    )
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("min_vol", mode="before")
    @classmethod
    def _parse_volume(cls, v: object) -> object:
        if isinstance(v, str):
            return Decimal(v.replace("_", "").replace(",", "").strip())
        return v

    @property
    def allowed_regimes(self) -> frozenset[MarketRegime]:
        """The regime gate set. Empty set ⇒ regime-agnostic (trade any regime)."""
        out: set[MarketRegime] = set()
        if self.trading_regime_1 is not None:
            out.add(self.trading_regime_1)
        if self.trading_regime_2 is not None:
            out.add(self.trading_regime_2)
        return frozenset(out)

    def passes_regime_gate(self, current: MarketRegime) -> bool:
        """OR semantics per Decision 016.

        Empty allowed set ⇒ always passes (regime-agnostic).
        Otherwise current must be in the set.
        """
        gate = self.allowed_regimes
        if not gate:
            return True
        return current in gate
