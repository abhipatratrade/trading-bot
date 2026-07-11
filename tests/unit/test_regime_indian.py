"""Indian regime — config load + retrain symbol-building (Phase 4/M5)."""

from __future__ import annotations

from pathlib import Path

from src.shared.regime.brain import RegimeConfig, load_regime_config
from src.shared.regime.retrain_job import _indian_symbols_to_train
from src.shared.regime.store import MARKET_SENTINEL

_REGIME_YAML = Path("src/strategies/swing/indian/regime.yaml")


def test_swing_indian_regime_yaml_is_nifty_proxy() -> None:
    cfg = load_regime_config(_REGIME_YAML)
    assert cfg.enabled is True
    assert cfg.proxy_symbol == "NIFTYBEES"
    assert cfg.tf == "1d"
    assert cfg.symbols == []  # broad-market only (market gate is bull/neutral)


def test_indian_symbols_direct_ticker_no_translation() -> None:
    cfg = RegimeConfig(
        proxy_symbol="NIFTYBEES", tf="1d",
        training_window_days=1100, inference_lookback_bars=250,
        symbols=["RELIANCE"],
    )
    pairs = _indian_symbols_to_train(cfg)
    # broad-market proxy first, then any per-name (fetch == bucket symbol)
    assert pairs == [(MARKET_SENTINEL, "NIFTYBEES"), ("RELIANCE", "RELIANCE")]
