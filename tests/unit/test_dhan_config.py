"""Dhan config resolution — Settings.dhan_account() (Phase 3/4)."""

from __future__ import annotations

import pytest

from src.core.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "trading_mode": "testnet",
        "database_url": "postgresql+psycopg://x",
        # Delta creds required by the model validator even for Dhan tests.
        "delta_testnet_api_key": "k",
        "delta_testnet_api_secret": "s",
        # Dhan refresh trio + sandbox order creds.
        "dhan_client_id": "1103267589",
        "dhan_pin": "990350",
        "dhan_totp_secret": "JBSWY3DPEHPK3PXP",
        "dhan_sandbox_client_id": "SBX123",
        "dhan_sandbox_access_token": "sbx-token",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_testnet_orders_go_to_sandbox() -> None:
    acct = _settings().dhan_account()
    assert acct.order_base_url == "https://sandbox.dhan.co"
    assert acct.order_client_id == "SBX123"
    assert acct.order_token == "sbx-token"
    # Data always live, with the TOTP refresh trio.
    assert acct.data_base_url == "https://api.dhan.co"
    assert acct.data_client_id == "1103267589"
    assert acct.pin == "990350"
    assert acct.totp_secret == "JBSWY3DPEHPK3PXP"


def test_live_orders_reuse_live_data_token() -> None:
    acct = _settings(
        trading_mode="live",
        delta_live_api_key="k",
        delta_live_api_secret="s",
    ).dhan_account()
    assert acct.order_base_url == "https://api.dhan.co"
    assert acct.order_client_id == "1103267589"
    assert acct.order_token is None  # signals "reuse the refreshed data token"


def test_testnet_missing_sandbox_creds_raises() -> None:
    with pytest.raises(ValueError, match="DHAN_SANDBOX"):
        _settings(dhan_sandbox_access_token=None).dhan_account()


def test_missing_data_auth_raises() -> None:
    with pytest.raises(ValueError, match="Dhan data auth missing"):
        _settings(dhan_pin=None, dhan_access_token=None).dhan_account()


def test_static_data_token_satisfies_data_auth() -> None:
    # No PIN/TOTP but a static access token → data auth OK.
    acct = _settings(dhan_pin=None, dhan_totp_secret=None,
                     dhan_access_token="static-live-tok").dhan_account()
    assert acct.data_token == "static-live-tok"
    assert acct.pin is None
