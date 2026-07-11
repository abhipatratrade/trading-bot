"""
Dhan access-token manager — TOTP auto-refresh.

Dhan capped directly-generated access tokens at 24h on 2025-10-01 (SEBI API
rule), so a pasted token goes stale mid-run. This mints a fresh ~24h token on
demand from the account's client id + login PIN + a live TOTP code
(``POST auth.dhan.co/app/generateAccessToken``), decoding the returned JWT's
``exp`` to refresh proactively before expiry.

Shared by the Dhan market-data adapter (``src.data_sources.dhan``) and the
broker client (``src.brokers.dhan.client``). Thread-safe: the bot's stop sweep
and tick loop can hit the same manager.

Two modes:
  * **Refreshable** — client_id + pin + totp_secret set: mints/refreshes the
    live token. Used for market data (always live) and live orders.
  * **Static** — only ``static_token`` set: returns it verbatim, never
    refreshes. Used for the DevPortal sandbox ORDER token, which the TOTP
    endpoint does not mint.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable

import httpx

from src.core.logging import get_logger

_AUTH_BASE = "https://auth.dhan.co"
_log = get_logger("brokers.dhan.auth")


def jwt_exp(token: str) -> int | None:
    """Best-effort parse of a JWT's ``exp`` (unix seconds); None if unparseable.

    Dhan access tokens are JWTs whose payload carries ``exp``. We only read it
    to schedule refresh — signature is not verified (the server does that).
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore base64 padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — malformed token → just don't schedule
        return None


class DhanTokenManager:
    """Provides a valid Dhan access token, refreshing via TOTP before expiry."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        pin: str | None = None,
        totp_secret: str | None = None,
        static_token: str | None = None,
        refresh_margin_seconds: int = 1800,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._pin = pin
        self._totp_secret = totp_secret
        self._margin = refresh_margin_seconds
        self._http = http or httpx.Client(timeout=30.0)
        self._owns_http = http is None
        self._clock = clock
        self._token: str | None = static_token
        self._exp: int | None = jwt_exp(static_token) if static_token else None
        self._lock = threading.Lock()

    @property
    def can_refresh(self) -> bool:
        return bool(self._client_id and self._pin and self._totp_secret)

    def token(self) -> str:
        """Return a currently-valid access token, refreshing if needed."""
        with self._lock:
            if self._needs_refresh():
                self._refresh_locked()
            if not self._token:
                raise RuntimeError(
                    "Dhan token unavailable: no static token and TOTP refresh "
                    "not configured (need client_id + pin + totp_secret)"
                )
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token so the next ``token()`` refreshes.

        Called by API clients after a 401/token-expiry response. No-op for a
        static token (nothing to refresh to).
        """
        with self._lock:
            if self.can_refresh:
                self._token = None
                self._exp = None

    # ── internals ──────────────────────────────────────────────────────
    def _needs_refresh(self) -> bool:
        if self._token is None:
            return self.can_refresh
        if not self.can_refresh:
            return False  # static token — never auto-refresh
        if self._exp is None:
            return False  # unknown expiry — rely on invalidate() after a 401
        return self._clock() >= self._exp - self._margin

    def _refresh_locked(self) -> None:
        import pyotp  # local import: only the refreshable path needs it

        code = pyotp.TOTP(self._totp_secret).now()
        r = self._http.post(
            f"{_AUTH_BASE}/app/generateAccessToken",
            params={"dhanClientId": self._client_id, "pin": self._pin, "totp": code},
        )
        r.raise_for_status()
        tok = r.json().get("accessToken")
        if not tok:
            raise RuntimeError(
                f"Dhan token refresh returned no accessToken: {r.text[:200]}"
            )
        self._token = tok
        self._exp = jwt_exp(tok)
        _log.info("dhan_token_refreshed", exp=self._exp)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
