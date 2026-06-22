"""Per-account Delta credential resolution (Decision 019)."""

from __future__ import annotations

import pytest

from src.core.config import Settings, TradingMode


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "trading_mode": "testnet",
        "database_url": "postgresql+psycopg://x",
        "delta_testnet_api_key": "default-key",
        "delta_testnet_api_secret": "default-secret",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_default_account_uses_top_level_keys() -> None:
    s = _settings()
    acct = s.delta_account("default")
    assert acct.api_key == "default-key"
    assert acct.api_secret == "default-secret"
    assert acct.base_url == s.delta_base_url


def test_named_account_resolves_its_own_keys() -> None:
    s = _settings(
        delta_swing_testnet_api_key="swing-key",
        delta_swing_testnet_api_secret="swing-secret",
    )
    acct = s.delta_account("swing")
    assert acct.api_key == "swing-key"
    assert acct.api_secret == "swing-secret"


def test_named_account_missing_keys_raises() -> None:
    s = _settings()  # no swing keys
    with pytest.raises(ValueError, match="account_ref='swing'"):
        s.delta_account("swing")


def test_live_mode_selects_live_keys() -> None:
    s = _settings(
        trading_mode="live",
        delta_live_api_key="live-key",
        delta_live_api_secret="live-secret",
        delta_gamble_live_api_key="gamble-live-key",
        delta_gamble_live_api_secret="gamble-live-secret",
    )
    assert s.trading_mode == TradingMode.LIVE
    assert s.delta_account("default").api_key == "live-key"
    assert s.delta_account("gamble").api_key == "gamble-live-key"


def test_default_and_named_accounts_are_distinct() -> None:
    s = _settings(
        delta_scalp_testnet_api_key="scalp-key",
        delta_scalp_testnet_api_secret="scalp-secret",
    )
    assert s.delta_account("default").api_key != s.delta_account("scalp").api_key
