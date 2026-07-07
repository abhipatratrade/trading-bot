"""Live USD/INR rate with cache, sanity clamp, and fallbacks (Phase 1c)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.data_sources import fx


@pytest.fixture(autouse=True)
def _clean_cache():
    fx.reset_fx_cache()
    yield
    fx.reset_fx_cache()


def test_good_fetch_is_used_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _fake_fetch() -> Decimal:
        calls["n"] += 1
        return Decimal("87.5")

    monkeypatch.setattr(fx, "_fetch_usd_inr", _fake_fetch)
    assert fx.get_usd_inr(fallback=Decimal("84")) == Decimal("87.5")
    assert fx.get_usd_inr(fallback=Decimal("84")) == Decimal("87.5")
    assert calls["n"] == 1  # second call served from cache


def test_failed_fetch_falls_back_to_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fx, "_fetch_usd_inr", lambda: None)
    assert fx.get_usd_inr(fallback=Decimal("84")) == Decimal("84")


def test_failed_fetch_prefers_last_good_over_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fx, "_fetch_usd_inr", lambda: Decimal("88"))
    fx.get_usd_inr(fallback=Decimal("84"))
    # Expire the cache, then break the API.
    rate, _ = fx._cache  # type: ignore[misc]
    fx._cache = (rate, -10**9)
    monkeypatch.setattr(fx, "_fetch_usd_inr", lambda: None)
    assert fx.get_usd_inr(fallback=Decimal("84")) == Decimal("88")


def test_insane_rate_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"rates": {"INR": 8400.0}}  # decimal-point bug upstream

    monkeypatch.setattr(fx.httpx, "get", lambda *a, **k: _Resp())
    assert fx._fetch_usd_inr() is None
