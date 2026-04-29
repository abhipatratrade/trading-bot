"""
Centralised configuration loaded from environment variables.

House rules enforced here:
- `TRADING_MODE` must be explicitly set to "testnet" or "live" — no default.
- Active Delta credentials are picked based on `TRADING_MODE`.
- Telegram + GDrive are optional; absent values disable those features
  rather than crash (so dev/testnet can run without them).

Usage:
    from src.core.config import settings
    if settings.trading_mode == "live":
        ...
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    TESTNET = "testnet"
    LIVE = "live"


class LogFormat(str, Enum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """Application settings — read from environment, validated at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Mode (required, no default) -----------------------------------------
    trading_mode: TradingMode = Field(
        ..., description="Must be 'testnet' or 'live'. No implicit default."
    )

    # -- Database ------------------------------------------------------------
    database_url: str = Field(..., description="SQLAlchemy URL")

    # -- Delta India (testnet) ----------------------------------------------
    delta_testnet_api_key: SecretStr | None = None
    delta_testnet_api_secret: SecretStr | None = None
    delta_testnet_base_url: str = "https://cdn-ind.testnet.deltaex.org"
    delta_testnet_ws_url: str = "wss://socket-ind.testnet.deltaex.org"

    # -- Delta India (live) -------------------------------------------------
    delta_live_api_key: SecretStr | None = None
    delta_live_api_secret: SecretStr | None = None
    delta_live_base_url: str = "https://api.india.delta.exchange"
    delta_live_ws_url: str = "wss://socket.india.delta.exchange"

    # -- Binance (public market data only) ----------------------------------
    binance_rest_url: str = "https://fapi.binance.com"
    binance_ws_url: str = "wss://fstream.binance.com"

    # -- Zerodha Kite -------------------------------------------------------
    kite_api_key: SecretStr | None = None
    kite_api_secret: SecretStr | None = None
    kite_access_token: SecretStr | None = None

    # -- Telegram (optional) ------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # -- Google Drive (optional, used by scheduler) -------------------------
    gdrive_service_account_json: str | None = None
    gdrive_folder_id: str | None = None

    # -- Logging ------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- Safety thresholds --------------------------------------------------
    daily_drawdown_pct: float = 5.0
    weekly_drawdown_pct: float = 10.0
    liquidation_distance_min_pct: float = 15.0
    drift_bps_max: float = 50.0

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_mode_credentials(self) -> "Settings":
        """Ensure the broker keys exist for the active mode.

        We deliberately do NOT raise if Telegram/GDrive are missing —
        those are optional and degrade to no-op.
        """
        if self.trading_mode == TradingMode.TESTNET:
            if not (self.delta_testnet_api_key and self.delta_testnet_api_secret):
                raise ValueError(
                    "TRADING_MODE=testnet requires DELTA_TESTNET_API_KEY "
                    "and DELTA_TESTNET_API_SECRET"
                )
        elif self.trading_mode == TradingMode.LIVE:
            if not (self.delta_live_api_key and self.delta_live_api_secret):
                raise ValueError(
                    "TRADING_MODE=live requires DELTA_LIVE_API_KEY "
                    "and DELTA_LIVE_API_SECRET"
                )
        return self

    # -----------------------------------------------------------------------
    # Convenience accessors — strategy code uses these, not the raw fields,
    # so the testnet/live switch is invisible to callers.
    # -----------------------------------------------------------------------
    @property
    def delta_api_key(self) -> SecretStr:
        key = (
            self.delta_testnet_api_key
            if self.trading_mode == TradingMode.TESTNET
            else self.delta_live_api_key
        )
        assert key is not None  # validator guarantees this
        return key

    @property
    def delta_api_secret(self) -> SecretStr:
        secret = (
            self.delta_testnet_api_secret
            if self.trading_mode == TradingMode.TESTNET
            else self.delta_live_api_secret
        )
        assert secret is not None
        return secret

    @property
    def delta_base_url(self) -> str:
        return (
            self.delta_testnet_base_url
            if self.trading_mode == TradingMode.TESTNET
            else self.delta_live_base_url
        )

    @property
    def delta_ws_url(self) -> str:
        return (
            self.delta_testnet_ws_url
            if self.trading_mode == TradingMode.TESTNET
            else self.delta_live_ws_url
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def gdrive_enabled(self) -> bool:
        return bool(self.gdrive_service_account_json and self.gdrive_folder_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor — single load per process.

    Lazy on purpose: importing this module must not blow up when env vars
    are unset (e.g., in unit tests). Production entrypoints call
    `get_settings()` once at startup; failure there is correct fail-fast
    behaviour.

    Tests should construct ``Settings(...)`` directly with overrides instead
    of relying on env vars, and call ``get_settings.cache_clear()`` if they
    have already triggered a load.
    """
    return Settings()  # type: ignore[call-arg]
