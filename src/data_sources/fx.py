"""
USD/INR FX rate with periodic refresh (Phase 1c).

The sizer converts INR bucket capital into USD-quoted contract counts and
the reconciler mirrors USD wallets into INR ``bucket_state`` rows. Both
used a static ``fx_inr_per_usd`` from ``allocator.yaml``; this module
layers a live rate on top:

    rate = live ECB rate (frankfurter.app, keyless), cached 12h,
           sanity-clamped to [50, 150],
           falling back to the last good fetch, then to the YAML value.

The YAML value stays as the ultimate fallback so the bot keeps sizing
sensibly through API outages (House Rule: no hard dependency on a
third-party for the trading loop to function).
"""

from __future__ import annotations

import time
from decimal import Decimal

import httpx

from src.core.logging import get_logger

_log = get_logger("data_sources.fx")

_FX_URL = "https://api.frankfurter.app/latest?from=USD&to=INR"
_TTL_SECONDS = 12 * 3600
# Reject obviously-broken API responses (USD/INR has lived in 60-90 for
# a decade; a value outside this band is a data error, not a market).
_SANE_MIN = Decimal("50")
_SANE_MAX = Decimal("150")

# (rate, fetched_at_monotonic) — last GOOD fetch, kept indefinitely as a
# better-than-YAML fallback when the API goes down.
_cache: tuple[Decimal, float] | None = None


def _fetch_usd_inr() -> Decimal | None:
    """One API call → rate, or None on any failure. Never raises."""
    try:
        resp = httpx.get(_FX_URL, timeout=10.0)
        if resp.status_code != 200:
            _log.warning("fx_fetch_bad_status", status=resp.status_code)
            return None
        rate = Decimal(str(resp.json()["rates"]["INR"]))
    except Exception:
        _log.warning("fx_fetch_failed", exc_info=True)
        return None
    if not (_SANE_MIN <= rate <= _SANE_MAX):
        _log.warning("fx_rate_insane_rejected", rate=str(rate))
        return None
    return rate


def get_usd_inr(fallback: Decimal) -> Decimal:
    """Current USD/INR: cached live rate, else last good fetch, else fallback."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[1] < _TTL_SECONDS:
        return _cache[0]

    rate = _fetch_usd_inr()
    if rate is not None:
        _cache = (rate, now)
        _log.info("fx_rate_refreshed", usd_inr=str(rate))
        return rate
    if _cache is not None:
        # Stale but real beats a hardcoded YAML constant.
        return _cache[0]
    return fallback


def reset_fx_cache() -> None:
    """Test helper."""
    global _cache
    _cache = None
