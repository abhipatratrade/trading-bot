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


def test_monday_morning_reaches_back_to_fridays_stub() -> None:
    """Before 10:15 the wanted bin is the previous TRADING day's stub.

    The previous *calendar* day would be Sunday, whose #6 bin cannot exist —
    so the Friday-stub → Monday-open entry (3 of the backtest's 214 trades)
    was unreachable. The 15:16 scan is the last of a session, so this morning
    pass is the only chance the stub bin ever gets.
    """
    monday = datetime(2026, 7, 27, 9, 20, tzinfo=_IST)
    assert monday.weekday() == 0
    assert meanrev.last_complete_bar_key(monday) == "2026-07-24#6"   # Friday

    # A listed NSE holiday is stepped over too: 2026-08-15 is a Saturday, so
    # Monday 2026-08-17 still reaches Friday the 14th.
    assert meanrev.last_complete_bar_key(
        datetime(2026, 8, 17, 9, 20, tzinfo=_IST)
    ) == "2026-08-14#6"
    # Tue 2026-01-27 sits behind Republic Day (Mon 26th) → Friday the 23rd.
    assert meanrev.last_complete_bar_key(
        datetime(2026, 1, 27, 9, 20, tzinfo=_IST)
    ) == "2026-01-23#6"


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


# ---------------------------------------------------------------------------
# Bar selection: the scanned bin is NOT the newest bin in the frame
#
# Regression cover for the bug that made this scanner structurally incapable of
# opening a position for its whole first live week (28 scans, 0 signals).
# bugfix_ref: Backtesting Engine/strategies/optimized/
#   midcap150_meanrev_1h_swing/MEANREV_1H_BUGFIX_HANDOFF.md
# ---------------------------------------------------------------------------
def _with_in_progress_bin(bin5_closes: float, in_progress_close: float):
    """30 flat sessions, then a last session whose bin 5 moved and bin 6 forms.

    Bars 20..23 are 14:15…15:00 (bin 5, the last COMPLETE bin at 15:16 IST);
    bar 24 is 15:15, the stub bin that Dhan is still filling in. This is the
    exact shape a live 15m fetch has when the scan fires at HH:16.
    """
    closes = [100.0] * 20 + [bin5_closes] * 4 + [in_progress_close]
    return _series_15m(30, 100.0, last_session_closes=closes)


def _last_day(bars) -> datetime:
    return max(bars, key=lambda b: b.timestamp).timestamp.astimezone(_IST)


def test_cross_fires_while_the_next_bin_is_still_forming() -> None:
    """BUG 1: the wanted bin is located, not required to be last in the frame.

    The scan fires at HH:16, one minute after the bin boundary, so the feed
    already carries the next (in-progress) bin. Requiring the wanted bin to be
    newest meant the guard never matched and every symbol returned None.
    """
    cfg = _cfg()
    intraday = _with_in_progress_bin(88.0, 100.0)   # bin 5 dislocated, bin 6 back up
    day = _last_day(intraday)
    daily = _daily(40, 100.0, end=day - timedelta(days=1))

    sig = meanrev.evaluate(
        "TEST", intraday, daily, cfg,
        want_bar_key=meanrev.bar_key(day.date(), 5),
    )
    assert sig is not None, "the completed bin's cross must still be found"
    assert sig.bar_key == meanrev.bar_key(day.date(), 5)
    assert sig.dist_pct < -cfg.dist_threshold
    # The in-progress bin 6 must not have leaked into the reading.
    assert sig.close == Decimal("88")


def test_in_progress_bin_cannot_manufacture_a_signal() -> None:
    """The mirror case: a poke down in the FORMING bin is not tradeable."""
    cfg = _cfg()
    intraday = _with_in_progress_bin(100.0, 88.0)   # bin 5 flat, bin 6 dumping
    day = _last_day(intraday)
    daily = _daily(40, 100.0, end=day - timedelta(days=1))
    assert meanrev.evaluate(
        "TEST", intraday, daily, cfg,
        want_bar_key=meanrev.bar_key(day.date(), 5),
    ) is None


def test_absent_bin_is_skipped_and_says_so() -> None:
    """A stale feed (or a name that did not trade that hour) is not entered."""
    cfg = _cfg()
    intraday = _with_in_progress_bin(88.0, 100.0)
    day = _last_day(intraday)
    daily = _daily(40, 100.0, end=day - timedelta(days=1))
    out = meanrev.evaluate_with_reason(
        "TEST", intraday, daily, cfg,
        want_bar_key=meanrev.bar_key(day.date() + timedelta(days=1), 3),
    )
    assert out.signal is None
    assert out.reason == meanrev.REASON_BIN_ABSENT
    assert not out.data_ok


def test_a_stray_later_bar_does_not_kill_the_scan() -> None:
    """Dhan emitted a lone Sat 2026-08-01 14:30 IST bar for many NSE names.

    Under the old "wanted bin must be last" rule that one bar made the newest
    bin ``2026-08-01#5`` and returned None for every symbol.
    """
    cfg = _cfg()
    intraday = _with_in_progress_bin(88.0, 100.0)
    day = _last_day(intraday)
    stray = day + timedelta(days=9)          # some later, unrelated session
    intraday = intraday + [_bar(stray.replace(hour=14, minute=30), 250.0)]
    daily = _daily(40, 100.0, end=day - timedelta(days=1))

    sig = meanrev.evaluate(
        "TEST", intraday, daily, cfg,
        want_bar_key=meanrev.bar_key(day.date(), 5),
    )
    assert sig is not None and sig.close == Decimal("88")


def test_outcome_reasons_name_the_guard_that_stopped_the_symbol() -> None:
    """The scan counts these; "0 of 94" must be attributable to a cause."""
    cfg = _cfg()
    day = _last_day(_series_15m(30, 100.0))

    flat = _series_15m(30, 100.0)
    assert meanrev.evaluate_with_reason(
        "TEST", flat, _daily(40, 100.0, end=day - timedelta(days=1)), cfg,
        want_bar_key=meanrev.bar_key(day.date(), 5),
    ).reason == meanrev.REASON_NO_CROSS

    cold = _series_15m(2, 100.0)
    cold_day = _last_day(cold)
    assert meanrev.evaluate_with_reason(
        "TEST", cold, _daily(40, 100.0, end=cold_day - timedelta(days=1)), cfg,
        want_bar_key=meanrev.bar_key(cold_day.date(), 5),
    ).reason == meanrev.REASON_COLD_EMA

    dislocated = _with_in_progress_bin(88.0, 100.0)
    assert meanrev.evaluate_with_reason(
        "TEST", dislocated, _daily(5, 100.0, end=day - timedelta(days=1)), cfg,
        want_bar_key=meanrev.bar_key(day.date(), 5),
    ).reason == meanrev.REASON_NO_DAILY_ATR


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
    day = _last_day(intraday)
    touched = meanrev.mean_touched(
        intraday, cfg.ema_len, want_bar_key=meanrev.bar_key(day.date(), 6)
    )
    assert touched is not None and touched[0] is True

    below = meanrev.mean_touched(
        _series_15m(30, 100.0, last_session_closes=[100] * 24 + [92.0]),
        cfg.ema_len,
        want_bar_key=meanrev.bar_key(day.date(), 6),
    )
    assert below is not None and below[0] is False


def test_mean_touch_ignores_a_poke_above_the_mean_in_the_forming_bin() -> None:
    """BUG 2: the exit used to read ``iloc[-1]`` — the bin still forming.

    Swept over the 2026-07-27..31 live week that flipped 161 exit decisions in
    both directions. Here bin 5 (complete) closed BELOW the mean and the
    forming bin 6 pokes above: the position must be held.
    """
    cfg = _cfg()
    intraday = _with_in_progress_bin(92.0, 108.0)
    day = _last_day(intraday)

    pinned = meanrev.mean_touched(
        intraday, cfg.ema_len, want_bar_key=meanrev.bar_key(day.date(), 5)
    )
    assert pinned is not None
    touched, close, _ema = pinned
    assert close == Decimal("92")
    assert touched is False, "an in-progress poke above the mean is not an exit"

    # Unpinned reads the forming bin and would have exited — the old behaviour.
    assert meanrev.mean_touched(intraday, cfg.ema_len)[0] is True


def test_mean_touch_returns_none_when_the_bin_is_absent() -> None:
    """No completed bin to read ⇒ no opinion, rather than a stale one."""
    cfg = _cfg()
    intraday = _series_15m(30, 100.0)
    day = _last_day(intraday)
    assert meanrev.mean_touched(
        intraday, cfg.ema_len,
        want_bar_key=meanrev.bar_key(day.date() + timedelta(days=1), 2),
    ) is None


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
