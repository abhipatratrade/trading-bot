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

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    TESTNET = "testnet"
    LIVE = "live"


def _enabled_bucket_brokers() -> set[str]:
    """Broker names used by currently ENABLED buckets, from ``buckets.yaml``.

    Read as raw YAML rather than via ``src.shared.bucket`` on purpose: this
    module sits at the bottom of the import graph, and ``shared.bucket`` pulls
    in ``core.models`` → ``core.db`` → back to ``core.config``. A plain file
    read keeps the layering intact.

    An unreadable or malformed file returns an EMPTY set, which skips the
    credential checks rather than crashing here. That is the right bias: a
    genuinely broken buckets.yaml is reported with a far better message by
    ``load_buckets()`` at startup, and this validator should not be the thing
    that fails on it.
    """
    try:
        import yaml

        path = Path(__file__).resolve().parents[2] / "buckets.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {
            str(cfg.get("broker"))
            for cfg in (raw.get("buckets") or {}).values()
            if isinstance(cfg, dict) and cfg.get("enabled") and cfg.get("broker")
        }
    except Exception:
        return set()


class DeltaAccount(NamedTuple):
    """Resolved Delta India credentials for one (sub-)account + mode."""

    api_key: str
    api_secret: str
    base_url: str
    ws_url: str


class DhanAccount(NamedTuple):
    """Resolved Dhan config for the active mode.

    Two token surfaces (see config notes): market DATA always hits the live
    ``data_base_url`` with the live account (TOTP-refreshed from
    ``data_client_id`` + ``pin`` + ``totp_secret``, or the static
    ``data_token`` fallback). ORDERS hit ``order_base_url`` with
    ``order_client_id`` + ``order_token`` — the DevPortal sandbox token in
    testnet, or the (refreshed) live token in live mode (``order_token`` is
    None then, signalling "reuse the data token").
    """

    data_base_url: str
    data_client_id: str | None
    data_token: str | None
    pin: str | None
    totp_secret: str | None
    order_base_url: str
    order_client_id: str | None
    order_token: str | None


class LogFormat(StrEnum):
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

    # -- Delta India sub-accounts (Decision 019) ----------------------------
    # One sub-account per crypto bucket so positions/leverage/margin are
    # isolated per bucket. ``account_ref: default`` (longterm-crypto) reuses
    # the keys above; the named accounts below are added per phase. base_url
    # / ws_url are shared (same exchange) and resolved from the active mode.
    delta_swing_testnet_api_key: SecretStr | None = None
    delta_swing_testnet_api_secret: SecretStr | None = None
    delta_swing_live_api_key: SecretStr | None = None
    delta_swing_live_api_secret: SecretStr | None = None

    delta_scalp_testnet_api_key: SecretStr | None = None
    delta_scalp_testnet_api_secret: SecretStr | None = None
    delta_scalp_live_api_key: SecretStr | None = None
    delta_scalp_live_api_secret: SecretStr | None = None

    delta_gamble_testnet_api_key: SecretStr | None = None
    delta_gamble_testnet_api_secret: SecretStr | None = None
    delta_gamble_live_api_key: SecretStr | None = None
    delta_gamble_live_api_secret: SecretStr | None = None

    # -- Binance (public market data only) ----------------------------------
    binance_rest_url: str = "https://fapi.binance.com"
    binance_ws_url: str = "wss://fstream.binance.com"

    # -- Dhan (DhanHQ API — stocks, Decision 012; Phase 3/4) ----------------
    # Access tokens are capped at 24h (SEBI, 2025-10-01), so the live data
    # token is auto-minted from client_id + PIN + TOTP each run rather than
    # pasted in. dhan_access_token is only an optional static fallback.
    #
    # Market DATA always uses the LIVE api.dhan.co (the sandbox has no data
    # feed). ORDERS go to sandbox.dhan.co (testnet) or api.dhan.co (live),
    # gated by trading_mode (House Rule #6).
    dhan_client_id: str | None = None          # live account client id
    dhan_access_token: SecretStr | None = None  # optional static live token
    dhan_pin: SecretStr | None = None           # login PIN (TOTP refresh)
    dhan_totp_secret: SecretStr | None = None   # base32 TOTP secret
    dhan_data_base_url: str = "https://api.dhan.co"
    dhan_sandbox_base_url: str = "https://sandbox.dhan.co"
    dhan_sandbox_client_id: str | None = None
    dhan_sandbox_access_token: SecretStr | None = None  # DevPortal sandbox token

    # -- Telegram (optional) ------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # -- Google Drive (optional, used by scheduler) -------------------------
    gdrive_service_account_json: str | None = None
    gdrive_folder_id: str | None = None

    # -- Logging ------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- Dashboard ----------------------------------------------------------
    dashboard_host: str = "0.0.0.0"  # noqa: S104
    dashboard_port: int = 8000
    # HTTP basic auth (Decision 021). When dashboard_password is unset the
    # dashboard serves WITHOUT auth and logs a warning — set it before
    # exposing the service anywhere reachable.
    dashboard_user: str = "admin"
    dashboard_password: SecretStr | None = None

    # -- Safety thresholds --------------------------------------------------
    daily_drawdown_pct: float = 5.0
    weekly_drawdown_pct: float = 10.0
    liquidation_distance_min_pct: float = 15.0
    funding_rate_max: float = 0.01
    drift_bps_max: float = 50.0
    # Dead-man's switch: the Railway scheduler pages when the bot-worker's
    # heartbeat row is older than this (bot beats every ~60s tick).
    heartbeat_stale_seconds: int = 600

    # -- Session invariants (src/safety/session_invariants.py) ---------------
    # Per-tick assertions that the session is BEHAVING. Breakers watch equity;
    # these watch process. A violation halts the bucket (kill switch) at most —
    # flattening stays with the breakers.
    # Grace after the 15:15 IST square-off before an intraday bucket that is
    # still holding counts as a failure. Small: the session closes at 15:30 and
    # the alert is only useful while there is time to act by hand.
    squareoff_grace_minutes: int = 5
    # The stop sweep runs every tick, so one uncovered reading can be a race
    # against a just-placed order. Two consecutive means it really failed.
    stop_coverage_sustain_ticks: int = 2
    # Headroom over capital × leverage before committed notional reads as a
    # sizing bug rather than rounding.
    notional_ceiling_tolerance: float = 1.10
    order_reject_window_minutes: int = 15
    order_reject_max: int = 3
    # A bucket is stalled once it has missed this many of its own cadences.
    bucket_stale_multiple: float = 3.0
    # ENFORCING since 2026-08-01 (Decision 033). Shipped observe-only on
    # 2026-07-28 and ran four sessions (28-31 Jul) with no false positive.
    #
    # Read that evidence honestly: those sessions carried ZERO positions, so
    # squareoff, stop_coverage and notional_ceiling were vacuous, and with zero
    # orders so was reject_rate. Only bucket_liveness was genuinely exercised
    # (clean), and foreign_positions fired as designed. Four of the six checks
    # will therefore act for the first time on the first day the bot holds
    # something. What bounds that risk is not the observe period — it is that
    # a trip HALTS only (Decision 024: exits, stops and breakers keep running)
    # and is reversible from the dashboard.
    session_invariants_enforcing: bool = True

    # -- Retention (nightly prune job on the Railway scheduler) --------------
    # audit_log keeps 3× longer — it's the forensic record (House Rule #8).
    snapshot_retention_days: int = 60
    audit_retention_days: int = 180

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_mode_credentials(self) -> Settings:
        """Ensure the broker keys exist for the active mode.

        Scoped to brokers an ENABLED bucket actually uses. Demanding Delta
        credentials when every Delta bucket is switched off blocks startup for
        an account the bot will never touch — which is exactly what happened on
        2026-07-22, when taking only intraday-indian (Dhan) live was refused
        for want of DELTA_LIVE_API_KEY.

        We deliberately do NOT raise if Telegram/GDrive are missing — those are
        optional and degrade to no-op. Dhan stays lazily validated in
        ``dhan_account()``, which raises with a precise message on first use.
        """
        brokers = _enabled_bucket_brokers()
        if "delta_india" in brokers:
            if self.trading_mode == TradingMode.TESTNET:
                if not (self.delta_testnet_api_key and self.delta_testnet_api_secret):
                    raise ValueError(
                        "TRADING_MODE=testnet requires DELTA_TESTNET_API_KEY "
                        "and DELTA_TESTNET_API_SECRET (a delta_india bucket is enabled)"
                    )
            elif self.trading_mode == TradingMode.LIVE:
                if not (self.delta_live_api_key and self.delta_live_api_secret):
                    raise ValueError(
                        "TRADING_MODE=live requires DELTA_LIVE_API_KEY "
                        "and DELTA_LIVE_API_SECRET (a delta_india bucket is enabled)"
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

    def delta_account(self, account_ref: str) -> DeltaAccount:
        """Resolve credentials for a Delta India (sub-)account in the active mode.

        ``account_ref == "default"`` returns the top-level keys (the original
        single account — used by longterm-crypto). Named refs resolve to
        ``delta_<ref>_<mode>_api_key`` / ``_api_secret`` fields. Raises
        ``ValueError`` (fail-fast, House Rule #6) if the keys for an account
        the bot actually needs are missing for the current mode.
        """
        mode = "testnet" if self.trading_mode == TradingMode.TESTNET else "live"
        if account_ref == "default":
            key_attr = f"delta_{mode}_api_key"
            secret_attr = f"delta_{mode}_api_secret"
        else:
            key_attr = f"delta_{account_ref}_{mode}_api_key"
            secret_attr = f"delta_{account_ref}_{mode}_api_secret"

        key: SecretStr | None = getattr(self, key_attr, None)
        secret: SecretStr | None = getattr(self, secret_attr, None)
        if key is None or secret is None:
            raise ValueError(
                f"Missing Delta credentials for account_ref={account_ref!r} "
                f"(mode={mode}); set {key_attr.upper()} and {secret_attr.upper()}"
            )
        return DeltaAccount(
            api_key=key.get_secret_value(),
            api_secret=secret.get_secret_value(),
            base_url=self.delta_base_url,
            ws_url=self.delta_ws_url,
        )

    def dhan_account(self) -> DhanAccount:
        """Resolve Dhan config for the active mode (fail-fast, House Rule #6).

        Requires the TOTP refresh trio (client_id + PIN + TOTP secret) OR a
        static live data token; and, in testnet, the sandbox order creds.
        """
        def _sv(s: SecretStr | None) -> str | None:
            return s.get_secret_value() if s is not None else None

        pin = _sv(self.dhan_pin)
        totp = _sv(self.dhan_totp_secret)
        data_token = _sv(self.dhan_access_token)
        can_refresh = bool(self.dhan_client_id and pin and totp)
        if not (can_refresh or data_token):
            raise ValueError(
                "Dhan data auth missing: set DHAN_CLIENT_ID + DHAN_PIN + "
                "DHAN_TOTP_SECRET (auto-refresh) or DHAN_ACCESS_TOKEN (static)"
            )

        if self.trading_mode == TradingMode.TESTNET:
            if not (self.dhan_sandbox_client_id and self.dhan_sandbox_access_token):
                raise ValueError(
                    "TRADING_MODE=testnet requires DHAN_SANDBOX_CLIENT_ID "
                    "and DHAN_SANDBOX_ACCESS_TOKEN"
                )
            order_base = self.dhan_sandbox_base_url
            order_client = self.dhan_sandbox_client_id
            order_token = _sv(self.dhan_sandbox_access_token)
        else:
            order_base = self.dhan_data_base_url
            order_client = self.dhan_client_id
            order_token = None  # live orders reuse the (refreshed) data token

        return DhanAccount(
            data_base_url=self.dhan_data_base_url,
            data_client_id=self.dhan_client_id,
            data_token=data_token,
            pin=pin,
            totp_secret=totp,
            order_base_url=order_base,
            order_client_id=order_client,
            order_token=order_token,
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
