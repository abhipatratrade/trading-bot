"""Tests for Decision 020 (VM-side retrain cadence) and the BNBUSD
training-mapping backfill."""

from __future__ import annotations

from datetime import UTC, datetime

from src.data_sources.symbol_loader import _apply_training_overrides
from src.shared.regime.retrain_job import _cadence_due_today


def test_override_backfills_blank_delta_symbol() -> None:
    rows = [
        {
            "canonical_symbol": "BNB",
            "binance_symbol": "BNBUSDT",
            "delta_symbol": "",
            "listed_on_binance": True,
            "listed_on_delta": False,
        }
    ]
    _apply_training_overrides(rows)
    bnb = rows[0]
    assert bnb["delta_symbol"] == "BNBUSD"
    assert bnb["binance_symbol"] == "BNBUSDT"
    # Not tradable on the current venue — left for the live fetch to set.
    assert bnb["listed_on_delta"] is False


def test_override_does_not_clobber_existing_mapping() -> None:
    rows = [
        {
            "canonical_symbol": "BNB",
            "binance_symbol": "BNBUSDT",
            "delta_symbol": "BNBUSD",
            "listed_on_binance": True,
            "listed_on_delta": True,
        }
    ]
    _apply_training_overrides(rows)
    assert rows[0]["delta_symbol"] == "BNBUSD"
    assert rows[0]["listed_on_delta"] is True


def test_override_adds_row_when_missing() -> None:
    rows: list[dict] = []
    _apply_training_overrides(rows)
    assert any(
        r["canonical_symbol"] == "BNB"
        and r["delta_symbol"] == "BNBUSD"
        and r["binance_symbol"] == "BNBUSDT"
        for r in rows
    )


def test_cadence_weekly_due_only_on_monday() -> None:
    monday = datetime(2026, 6, 22, 2, 0, tzinfo=UTC)
    tuesday = datetime(2026, 6, 23, 2, 0, tzinfo=UTC)
    assert monday.weekday() == 0 and tuesday.weekday() == 1
    assert _cadence_due_today("weekly", monday) is True
    assert _cadence_due_today("weekly", tuesday) is False


def test_cadence_daily_always_and_manual_never() -> None:
    any_day = datetime(2026, 6, 23, 2, 0, tzinfo=UTC)
    assert _cadence_due_today("daily", any_day) is True
    assert _cadence_due_today("manual", any_day) is False
    assert _cadence_due_today("unknown", any_day) is False
