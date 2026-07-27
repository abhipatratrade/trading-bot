"""Indian regime — config load + retrain symbol-building (Phase 4/M5)."""

from __future__ import annotations

from pathlib import Path

from src.shared.regime.brain import RegimeConfig, load_regime_config
from src.shared.regime.retrain_job import _indian_symbols_to_train
from src.shared.regime.store import MARKET_SENTINEL

_REGIME_YAML = Path("src/strategies/swing/indian/regime.yaml")


def test_swing_indian_regime_gate_is_off() -> None:
    """Decision 032: the 1h mean-reversion strategy must NOT be regime-gated.

    It buys panic dislocations, so a bull/neutral gate mutes it exactly when it
    works — in-sample the gate cut net +30.7% → +2–9%. The proxy config is kept
    for a future re-enable but must stay disabled.
    """
    cfg = load_regime_config(_REGIME_YAML)
    assert cfg.enabled is False
    assert cfg.proxy_symbol == "NIFTYBEES"
    assert cfg.tf == "1d"
    assert cfg.symbols == []


def test_indian_symbols_direct_ticker_no_translation() -> None:
    cfg = RegimeConfig(
        proxy_symbol="NIFTYBEES", tf="1d",
        training_window_days=1100, inference_lookback_bars=250,
        symbols=["RELIANCE"],
    )
    pairs = _indian_symbols_to_train(cfg)
    # broad-market proxy first, then any per-name (fetch == bucket symbol)
    assert pairs == [(MARKET_SENTINEL, "NIFTYBEES"), ("RELIANCE", "RELIANCE")]
