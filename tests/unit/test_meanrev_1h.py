"""Midcap-150 1h mean reversion — series construction, signal, exits (Decision 032).

The arithmetic here has to match the Backtesting Engine's
``mean_reversion_1h_scanner`` bar-for-bar, so most of these tests are parity
assertions against that engine's definitions rather than "does it run" checks:
the 09:15-anchored resample, the FRESH-cross rule, the scale guard, and the
prior-close daily ATR.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.data_sources.base import OHLCVBar
from src.shared.scanner import meanrev
from src.shared.scanner.engine import load_scanner_config
from src.strategies.swing.indian.strategies.mean_reversion_1h import (
    MeanReversion1h,
    _trading_days_between,
)

_SCANNER_YAML = Path("src/strategies/swing/indian/scanner.yaml")
_IST = meanrev.IST


def _cfg() -> meanrev.MeanRevConfig:
    return meanrev.MeanRevConfig.from_scanner_config(
        load_scanner_config(_SCANNER_YAML)
    )


def _bar(ts: datetime, close: float, *, high: float | None = None,
         low: float | None = None) -> OHLCVBar:
    return OHLCVBar(
        timestamp=ts,
        open=Decimal(str(close)),
        high=Decimal(str(high if high is not None else close * 1.002)),
        low=Decimal(str(low if low is not None else close * 0.998)),
        close=Decimal(str(close)),
        volume=Decimal("10000"),
    )


def _session_15m(day: datetime, closes: list[float]) -> list[OHLCVBar]:
    """15m bars from 09:15 IST for one session (25 slots = 09:15…15:15)."""
    start = day.replace(hour=9, minute=15, second=0, microsecond=0, tzinfo=_IST)
    return [_bar(start + timedelta(minutes=15 * i), c) for i, c in enumerate(closes)]


def _series_15m(days: int, price: float, *, last_session_closes=None) -> list[OHLCVBar]:
    """``days`` flat sessions of 25 bars each, oldest first (weekdays only)."""
    bars: list[OHLCVBar] = []
    d = datetime(2026, 3, 2, tzinfo=_IST)  # a Monday
    made = 0
    while made < days:
        if d.weekday() < 5:
            closes = [price] * 25
            if last_session_closes is not None and made == days - 1:
                closes = last_session_closes
            bars.extend(_session_15m(d, closes))
            made += 1
        d += timedelta(days=1)
    return bars


def _daily(days: int, price: float, *, end: datetime) -> list[OHLCVBar]:
    out: list[OHLCVBar] = []
    d = end
    made = 0
    while made < days:
        if d.weekday() < 5:
            out.append(
                _bar(d.replace(hour=15, minute=30, tzinfo=_IST), price,
                     high=price * 1.02, low=price * 0.98)
            )
            made += 1
        d -= timedelta(days=1)
    return sorted(out, key=lambda b: b.timestamp)


# ---------------------------------------------------------------------------
# Resample: 09:15-anchored 1h bins
# ---------------------------------------------------------------------------
def test_resample_bins_a_session_into_seven_1h_bars() -> None:
    """25 15m bars → 7 bins; the last is the 15:15→15:30 stub of ONE bar."""
    day = datetime(2026, 7, 20, tzinfo=_IST)
    closes = [100 + i for i in range(25)]
    h1 = meanrev.resample_1h(_session_15m(day, closes))
    assert len(h1) == 7
    assert list(h1["bin"]) == [0, 1, 2, 3, 4, 5, 6]
    # bin 0 = 09:15,09:30,09:45,10:00 → OHLC first/max/min/last of those four
    assert h1["open"].iloc[0] == 100.0
    assert h1["close"].iloc[0] == 103.0
    assert h1["close"].iloc[5] == 123.0        # 14:15…15:00
    assert h1["close"].iloc[6] == 124.0        # the 15:15 stub, alone


def test_resample_is_anchored_per_day_not_rolling() -> None:
    """A new session restarts the bin numbering (09:15 is always bin 0)."""
    bars = _session_15m(datetime(2026, 7, 20, tzinfo=_IST), [100] * 25)
    bars += _session_15m(datetime(2026, 7, 21, tzinfo=_IST), [101] * 25)
    h1 = meanrev.resample_1h(bars)
    assert len(h1) == 14
    assert list(h1["bin"])[:7] == [0, 1, 2, 3, 4, 5, 6]
    assert list(h1["bin"])[7:] == [0, 1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------------------
# Bar keys: which bin the scan is scanning for
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("hh", "mm", "expect_bin"),
    [
        (10, 16, 0),   # first bin just closed
        (11, 20, 1),
        (15, 16, 5),   # 14:15→15:15 closed
        (15, 29, 5),   # the stub is still forming
    ],
)
def test_last_complete_bar_key_during_session(hh: int, mm: int, expect_bin: int) -> None:
    now = datetime(2026, 7, 20, hh, mm, tzinfo=_IST)
    assert meanrev.last_complete_bar_key(now) == f"2026-07-20#{expect_bin}"


def test_stub_bin_is_actionable_until_the_next_bin_closes() -> None:
    """A 15:15→15:30 signal stays the newest bin through the next 09:15 open.

    This is what reproduces the backtest's three 09:15 entries: its fill is the
    open of the bar AFTER the signal bar, and for the stub that bar is the next
    session's first one.
    """
    after_close = datetime(2026, 7, 20, 15, 45, tzinfo=_IST)
    assert meanrev.last_complete_bar_key(after_close) == "2026-07-20#6"
    next_morning = datetime(2026, 7, 21, 9, 20, tzinfo=_IST)
    assert meanrev.last_complete_bar_key(next_morning) == "2026-07-20#6"
    # …and goes stale once 10:15 closes bin 0 of the new session.
    assert meanrev.last_complete_bar_key(
        datetime(2026, 7, 21, 10, 20, tzinfo=_IST)
    ) == "2026-07-21#0"


# ---------------------------------------------------------------------------
# Signal: FRESH cross only
# ---------------------------------------------------------------------------
def _dislocated_series(drop_pct: float, *, prior_drop_pct: float = 0.0):
    """Flat history, then the last session's final two bins step down.

    ``prior_drop_pct`` puts the PREVIOUS bin already below the band, which is
    what distinguishes a fresh cross from a stock that is simply sitting
    stretched.
    """
    price = 100.0
    closes = [price] * 25
    if prior_drop_pct:
        closes[20:24] = [price * (1 - prior_drop_pct / 100)] * 4
    closes[24] = price * (1 - drop_pct / 100)
    return _series_15m(30, price, last_session_closes=closes)


def test_fresh_cross_below_the_band_fires() -> None:
    cfg = _cfg()
    intraday = _dislocated_series(9.0)
    last = max(intraday, key=lambda b: b.timestamp)
    daily = _daily(40, 100.0, end=last.timestamp.astimezone(_IST) - timedelta(days=1))
    key = meanrev.bar_key(last.timestamp.astimezone(_IST).date(), 6)

    sig = meanrev.evaluate("TEST", intraday, daily, cfg, want_bar_key=key)
    assert sig is not None
    assert sig.dist_pct < -cfg.dist_threshold
    assert sig.bar_key == key
    # stop distance = 3.5 × daily ATR14, in rupees
    assert sig.stop_distance == sig.atr14 * Decimal("3.5")


def test_shallow_dislocation_does_not_fire() -> None:
    """4.5% was the original spec and is net-negative; only ≥6.5% trades."""
    cfg = _cfg()
    intraday = _dislocated_series(4.5)
    last = max(intraday, key=lambda b: b.timestamp)
    daily = _daily(40, 100.0, end=last.timestamp.astimezone(_IST) - timedelta(days=1))
    key = meanrev.bar_key(last.timestamp.astimezone(_IST).date(), 6)
    assert meanrev.evaluate("TEST", intraday, daily, cfg, want_bar_key=key) is None


def test_already_stretched_symbol_does_not_re_fire() -> None:
    """dist[t−1] must be ABOVE the band — a stuck-stretched name is not fresh."""
    cfg = _cfg()
    intraday = _dislocated_series(9.5, prior_drop_pct=9.0)
    last = max(intraday, key=lambda b: b.timestamp)
    daily = _daily(40, 100.0, end=last.timestamp.astimezone(_IST) - timedelta(days=1))
    key = meanrev.bar_key(last.timestamp.astimezone(_IST).date(), 6)
    assert meanrev.evaluate("TEST", intraday, daily, cfg, want_bar_key=key) is None


def test_signal_on_an_older_bin_is_skipped() -> None:
    """A hit found late (restart mid-session) is not entered at a stale price."""
    cfg = _cfg()
    intraday = _dislocated_series(9.0)
    last = max(intraday, key=lambda b: b.timestamp)
    daily = _daily(40, 100.0, end=last.timestamp.astimezone(_IST) - timedelta(days=1))
    stale = meanrev.bar_key(last.timestamp.astimezone(_IST).date(), 3)
    assert meanrev.evaluate("TEST", intraday, daily, cfg, want_bar_key=stale) is None


def test_short_history_never_signals() -> None:
    """EMA20 needs a warm series; a cold one must not trade on a seeded value."""
    cfg = _cfg()
    intraday = _series_15m(2, 100.0, last_session_closes=[100] * 24 + [88.0])
    last = max(intraday, key=lambda b: b.timestamp)
    daily = _daily(40, 100.0, end=last.timestamp.astimezone(_IST) - timedelta(days=1))
    key = meanrev.bar_key(last.timestamp.astimezone(_IST).date(), 6)
    assert meanrev.evaluate("TEST", intraday, daily, cfg, want_bar_key=key) is None


# ---------------------------------------------------------------------------
# Scale guard + ATR
# ---------------------------------------------------------------------------
def test_scale_guard_rejects_a_split_adjusted_daily_series() -> None:
    """Unadjusted intraday vs adjusted daily at 2:1 is not a −50% dislocation."""
    intraday = _series_15m(30, 100.0)
    last = max(intraday, key=lambda b: b.timestamp)
    daily = _daily(40, 50.0, end=last.timestamp.astimezone(_IST))
    assert not meanrev.scales_consistent(intraday, daily, Decimal("0.03"))
    assert meanrev.scales_consistent(
        intraday, _daily(40, 100.0, end=last.timestamp.astimezone(_IST)),
        Decimal("0.03"),
    )


def test_scale_guard_allows_thin_overlap() -> None:
    """Under 20 overlapping sessions there is not enough evidence to reject."""
    intraday = _series_15m(5, 100.0)
    last = max(intraday, key=lambda b: b.timestamp)
    daily = _daily(5, 50.0, end=last.timestamp.astimezone(_IST))
    assert meanrev.scales_consistent(intraday, daily, Decimal("0.03"))


def test_daily_atr_excludes_the_signal_day() -> None:
    """ATR is "as of the PRIOR close" — today's half-formed candle can't leak in."""
    end = datetime(2026, 7, 20, tzinfo=_IST)
    bars = _daily(30, 100.0, end=end)
    bars.append(_bar(end.replace(hour=12, tzinfo=_IST), 1000.0,
                     high=2000.0, low=10.0))  # absurd in-progress bar for today
    atr = meanrev.daily_atr(bars, end.date(), 14)
    assert atr is not None
    assert atr < Decimal("10")   # the 1990-wide today bar was not counted


def test_daily_atr_none_without_enough_history() -> None:
    end = datetime(2026, 7, 20, tzinfo=_IST)
    assert meanrev.daily_atr(_daily(5, 100.0, end=end), end.date(), 14) is None


# ---------------------------------------------------------------------------
# Ranking + exits
# ---------------------------------------------------------------------------
def test_rank_takes_the_deepest_dislocations() -> None:
    def _s(sym: str, dist: str) -> meanrev.MeanRevSignal:
        return meanrev.MeanRevSignal(
            symbol=sym, bar_key="2026-07-20#0",
            bar_close_utc=datetime(2026, 7, 20, 4, 45, tzinfo=UTC),
            close=Decimal("100"), ema20=Decimal("110"),
            dist_pct=Decimal(dist), atr14=Decimal("3"),
            stop_distance=Decimal("10.5"),
        )
    ranked = meanrev.rank_top(
        [_s("A", "-7.0"), _s("B", "-12.0"), _s("C", "-9.0")], 2
    )
    assert [s.symbol for s in ranked] == ["B", "C"]


def test_mean_touch_fires_only_on_a_completed_bin() -> None:
    """Close ≥ EMA20 on the last COMPLETE 1h bar is the primary exit."""
    cfg = _cfg()
    # Recover: flat at 100, last bin closes at 108 → above a ~100 EMA.
    intraday = _series_15m(30, 100.0, last_session_closes=[100] * 24 + [108.0])
    touched = meanrev.mean_touched(intraday, cfg.ema_len)
    assert touched is not None and touched[0] is True

    below = meanrev.mean_touched(
        _series_15m(30, 100.0, last_session_closes=[100] * 24 + [92.0]),
        cfg.ema_len,
    )
    assert below is not None and below[0] is False


def test_max_hold_is_counted_in_trading_days() -> None:
    """20 TRADING days, not calendar — weekends/holidays must not close early."""
    start = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)      # Monday
    assert _trading_days_between(start, start + timedelta(days=7)) == 5
    assert _trading_days_between(start, start + timedelta(days=28)) == 20
    assert _trading_days_between(start, start) == 0


# ---------------------------------------------------------------------------
# Config parity: the strategy's frozen constants vs scanner.yaml
# ---------------------------------------------------------------------------
def test_strategy_constants_match_scanner_yaml() -> None:
    """The strategy re-derives the signal, so its constants must not drift."""
    yaml_cfg = _cfg()
    strat_cfg = MeanReversion1h()._cfg()
    assert strat_cfg.ema_len == yaml_cfg.ema_len
    assert strat_cfg.dist_threshold == yaml_cfg.dist_threshold
    assert strat_cfg.atr_period == yaml_cfg.atr_period
    assert strat_cfg.stop_atr_mult == yaml_cfg.stop_atr_mult
    assert strat_cfg.max_hold_days == yaml_cfg.max_hold_days
    assert strat_cfg.intraday_lookback_days == yaml_cfg.intraday_lookback_days
    assert strat_cfg.scale_tolerance == yaml_cfg.scale_tolerance


def test_frozen_backtest_parameters() -> None:
    """Guardrail: these are the holdout-validated values. Changing one needs a
    fresh backtest_ref, not an edit."""
    cfg = _cfg()
    assert cfg.dist_threshold == Decimal("6.5")
    assert cfg.ema_len == 20
    assert cfg.stop_atr_mult == Decimal("3.5")
    assert cfg.atr_period == 14
    assert cfg.max_hold_days == 20
    assert cfg.daily_entry_cap == 5
    assert cfg.universe_size == 5
    assert cfg.fno_only is True
    assert len(cfg.symbols) == 94
