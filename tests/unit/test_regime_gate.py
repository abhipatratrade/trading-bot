"""Tests for the OR-semantics regime gate (Decision 016)."""

from __future__ import annotations

from decimal import Decimal

from src.core.models import MarketRegime
from src.shared.strategy_master.schema import StrategyMasterRow


def _row(r1, r2):
    return StrategyMasterRow(
        strategy_name="x",
        tf="1d",
        min_vol=Decimal("0"),
        trading_regime_1=r1,
        trading_regime_2=r2,
        trading_type="longterm",
    )


class TestRegimeGate:
    def test_both_blank_is_regime_agnostic(self) -> None:
        row = _row(None, None)
        for r in MarketRegime:
            assert row.passes_regime_gate(r) is True

    def test_single_filled_slot(self) -> None:
        row = _row(MarketRegime.BULL, None)
        assert row.passes_regime_gate(MarketRegime.BULL) is True
        assert row.passes_regime_gate(MarketRegime.BEAR) is False
        assert row.passes_regime_gate(MarketRegime.NEUTRAL) is False

    def test_or_semantics(self) -> None:
        # PPTX-style: trade in bull OR neutral
        row = _row(MarketRegime.BULL, MarketRegime.NEUTRAL)
        assert row.passes_regime_gate(MarketRegime.BULL) is True
        assert row.passes_regime_gate(MarketRegime.NEUTRAL) is True
        assert row.passes_regime_gate(MarketRegime.BEAR) is False

    def test_allowed_set_is_frozen(self) -> None:
        row = _row(MarketRegime.BULL, MarketRegime.BEAR)
        gate = row.allowed_regimes
        assert isinstance(gate, frozenset)
        assert gate == frozenset({MarketRegime.BULL, MarketRegime.BEAR})

    def test_blank_strings_become_none(self) -> None:
        # Simulates CSV cell with empty string
        row = StrategyMasterRow.model_validate(
            {
                "strategy_name": "x",
                "tf": "1d",
                "min_vol": "",
                "trading_regime_1": "",
                "trading_regime_2": "",
                "trading_type": "longterm",
            }
        )
        assert row.trading_regime_1 is None
        assert row.trading_regime_2 is None
        assert row.min_vol is None
        assert row.passes_regime_gate(MarketRegime.BULL) is True
