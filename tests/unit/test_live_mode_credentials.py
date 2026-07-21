"""Live-mode credential validation is scoped to ENABLED buckets.

Regression for 2026-07-22: taking only intraday-indian (Dhan) live was refused
because the validator demanded DELTA_LIVE_API_KEY unconditionally — for a
Delta account no enabled bucket would ever touch.
"""

from __future__ import annotations

import pytest

from src.core.config import Settings, _enabled_bucket_brokers


def _settings(monkeypatch: pytest.MonkeyPatch, **over: object) -> Settings:
    """Hermetic Settings — no repo .env, no ambient DELTA_* env vars.

    Both matter: the repo root carries a real .env, and the suite has been
    bitten before by tests silently reading it (2026-07-12). Without this a
    "missing credentials" assertion passes or fails depending on whose
    machine it runs on.
    """
    for var in (
        "DELTA_TESTNET_API_KEY",
        "DELTA_TESTNET_API_SECRET",
        "DELTA_LIVE_API_KEY",
        "DELTA_LIVE_API_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    base: dict[str, object] = {
        "trading_mode": "live",
        "database_url": "postgresql://x/y",
        "_env_file": None,
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_live_without_delta_keys_is_fine_when_no_delta_bucket_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.core.config._enabled_bucket_brokers", lambda: {"dhan"}
    )
    s = _settings(monkeypatch)
    assert s.trading_mode.value == "live"


def test_live_still_requires_delta_keys_when_a_delta_bucket_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.core.config._enabled_bucket_brokers", lambda: {"delta_india"}
    )
    with pytest.raises(ValueError, match="DELTA_LIVE_API_KEY"):
        _settings(monkeypatch)


def test_testnet_still_requires_delta_keys_when_a_delta_bucket_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.core.config._enabled_bucket_brokers", lambda: {"delta_india"}
    )
    with pytest.raises(ValueError, match="DELTA_TESTNET_API_KEY"):
        _settings(monkeypatch, trading_mode="testnet")


def test_no_enabled_buckets_requires_no_broker_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid rollout stage: everything paused, nothing to authenticate."""
    monkeypatch.setattr("src.core.config._enabled_bucket_brokers", lambda: set())
    assert _settings(monkeypatch).trading_mode.value == "live"


def test_helper_reads_the_real_buckets_yaml() -> None:
    """Whatever is enabled must resolve to known broker names."""
    assert _enabled_bucket_brokers() <= {"delta_india", "dhan"}
