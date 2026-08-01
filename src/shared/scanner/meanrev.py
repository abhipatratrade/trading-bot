"""
1h mean-reversion scan logic — Midcap-150 F&O dislocation buy (Decision 032).

backtest_ref: ``Backtesting Engine/strategies/optimized/midcap150_meanrev_1h_swing/``
  (TRADING_BOT_HANDOFF.md + mean_reversion_1h_swing_rules.json; engine
  ``mean_reversion_1h_scanner.py``, whose ``MeanRevConfig`` defaults ARE the
  frozen live config).

bugfix_ref: same folder, ``MEANREV_1H_BUGFIX_HANDOFF.md`` (2026-08-01). Bar
  SELECTION, not bar math, was wrong: both the entry and the exit read the bin
  still forming rather than the bin being scanned. See :func:`locate_bin` and
  :func:`mean_touched`. The signal arithmetic was never at fault — it agreed
  with the engine to 4dp throughout.

The signal is a **fresh downward cross** of a distance band below the 1h EMA20:

    ema20 = EMA(20) over the CONTINUOUS 1h close series
    dist  = (close / ema20 − 1) × 100
    fire  = dist[t] ≤ −threshold AND dist[t−1] > −threshold

"Fresh" is what stops a stock that simply *sits* 8% stretched from re-firing on
every bar: it re-arms only after ``dist`` climbs back above the band.

Two things about the series matter more than the arithmetic:

* **The 1h bars are RESAMPLED from Dhan 15m, anchored per IST day to 09:15** —
  the same construction as a TradingView 1h chart, and byte-for-byte the
  backtest's ``scanner_engine._resample_intraday``. A session therefore has 7
  bins, the last of which (15:15→15:30) is a 15-minute stub. That stub is a real
  signal bar in the backtest (3 of 214 trades entered at the following 09:15
  open), so it is one here too.
* **Dhan intraday is UNADJUSTED, daily is ADJUSTED.** The 1h signal lives on the
  intraday series and the ATR stop on the daily series, and ``scales_consistent``
  drops any name whose two series have drifted apart in scale (split/bonus)
  rather than trading a fabricated −6.5% dislocation.

Pure functions over bar lists — no DB, no broker (importable by the backtester,
House Rule 9). The engine branch in ``scanner/engine.py`` and the strategy in
``strategies/swing/indian/strategies/mean_reversion_1h.py`` wrap them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from datetime import date as date_type
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd

from src.shared.scanner import indicators as ind

if TYPE_CHECKING:
    from src.data_sources.base import OHLCVBar
    from src.shared.scanner.engine import ScannerConfig

IST = timezone(timedelta(hours=5, minutes=30))

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
# Minutes per resample bin. 1h bins are numbered from the 09:15 open, so bin 6
# (15:15→15:30) is a stub — see the module docstring.
_BIN_MINUTES = 60
_BINS_PER_SESSION = 7

# Overlapping sessions needed before the adjusted/unadjusted scale guard is
# allowed to reject anything (matches the backtest's ``_scales_consistent``).
_SCALE_MIN_OVERLAP = 20

# How far back ``last_complete_bar_key`` will walk looking for the previous
# trading day. Comfortably clears a long Diwali/holiday weekend; if the walk
# somehow runs out the key names a non-trading day and the scan simply finds
# no bin, which is the safe direction.
_MAX_MARKET_GAP_DAYS = 10


@dataclass(frozen=True, slots=True)
class MeanRevConfig:
    """Thresholds parsed from the bucket's ``scanner.yaml``."""

    universe_size: int            # max NEW entries per scan (top-N by depth)
    symbols: tuple[str, ...]      # the pinned Midcap-150 ∩ F&O list
    ema_len: int
    dist_threshold: Decimal       # enter when dist ≤ −this (percent)
    atr_period: int
    stop_atr_mult: Decimal        # protective stop = entry − mult × daily ATR
    max_hold_days: int            # trading-day backstop
    daily_entry_cap: int          # max NEW entries per session (portfolio rule)
    intraday_lookback_days: int   # 15m history pulled for the 1h series
    scale_tolerance: Decimal      # unadjusted-vs-adjusted divergence allowed
    fno_only: bool                # require an F&O underlying

    @classmethod
    def from_scanner_config(cls, config: ScannerConfig) -> MeanRevConfig:
        """Read thresholds out of the named filters (order-independent)."""
        by_name = {f.name: f.params for f in config.filters}

        def _p(name: str, key: str, default: object) -> Decimal:
            return Decimal(str(by_name.get(name, {}).get(key, default)))

        def _i(name: str, key: str, default: int) -> int:
            return int(by_name.get(name, {}).get(key, default))

        def _b(name: str, key: str, default: bool) -> bool:
            return bool(by_name.get(name, {}).get(key, default))

        return cls(
            universe_size=config.universe_size,
            symbols=tuple(config.symbols),
            ema_len=_i("ema_distance_cross", "ema_len", 20),
            dist_threshold=_p("ema_distance_cross", "threshold_pct", 6.5),
            atr_period=_i("protective_stop", "atr_period", 14),
            stop_atr_mult=_p("protective_stop", "atr_mult", 3.5),
            max_hold_days=_i("max_hold", "trading_days", 20),
            daily_entry_cap=_i("daily_entry_cap", "max_new_per_day", 5),
            intraday_lookback_days=_i("history", "intraday_lookback_days", 90),
            scale_tolerance=_p("scale_guard", "tolerance", 0.03),
            fno_only=_b("fno_only", "required", True),
        )


@dataclass(frozen=True, slots=True)
class MeanRevSignal:
    """A symbol whose last COMPLETE 1h bin freshly crossed below the band."""

    symbol: str
    bar_key: str                 # "<IST date>#<bin>" of the signal bar
    bar_close_utc: datetime      # when that bin finished (entry is the next open)
    close: Decimal
    ema20: Decimal
    dist_pct: Decimal            # negative; deeper = better
    atr14: Decimal               # DAILY ATR as of the prior settled close
    stop_distance: Decimal       # stop_atr_mult × atr14, in rupees


# Why a symbol did not produce a signal. Mirrors the gap-reversal screen's
# convention (Decision 033): everything prefixed ``data_`` means we could NOT
# evaluate the symbol, everything else means we evaluated it and it did not
# qualify. Counting these post-``evaluate`` is what distinguishes "94 names
# checked, none crossed" from "94 names bailed at the first guard" — the two
# were indistinguishable in the audit log, which is how the bar-selection bug
# below survived its first live week.
REASON_OK = ""
REASON_BIN_ABSENT = "data_bin_absent"
REASON_COLD_EMA = "data_ema_warmup"
REASON_NO_DAILY_ATR = "data_no_daily_atr"
REASON_SCALE_REJECT = "scale_mismatch"
REASON_NO_CROSS = "no_fresh_cross"


@dataclass(frozen=True, slots=True)
class MeanRevOutcome:
    """The scan's verdict for one symbol plus what it computed getting there."""

    signal: MeanRevSignal | None
    reason: str
    metrics: dict[str, str]

    @property
    def data_ok(self) -> bool:
        """True when the symbol was actually evaluable, whatever the verdict."""
        return not self.reason.startswith("data_")


# ---------------------------------------------------------------------------
# Bar-time helpers
# ---------------------------------------------------------------------------
def ist_dt(bar: OHLCVBar) -> datetime:
    return bar.timestamp.astimezone(IST)


def ist_date(bar: OHLCVBar) -> date_type:
    return bar.timestamp.astimezone(IST).date()


def bin_index(moment: datetime) -> int:
    """Which 1h bin an IST moment falls in (0 = 09:15→10:15, 6 = the stub).

    Values outside the session clamp to the nearest end — the caller decides
    what that means (``bar_key_at`` uses it to name the bin currently forming).
    """
    minutes = (moment.hour - SESSION_OPEN.hour) * 60 + (
        moment.minute - SESSION_OPEN.minute
    )
    if minutes < 0:
        return -1
    return min(minutes // _BIN_MINUTES, _BINS_PER_SESSION - 1)


def bar_key(day: date_type, index: int) -> str:
    return f"{day.isoformat()}#{index}"


def last_complete_bar_key(now: datetime) -> str | None:
    """Key of the most recently COMPLETED 1h bin as of ``now``.

    This is the scan's cache key: within one bin the signal cannot change, so a
    60s tick loop recomputes only when this value moves. Before the first bin
    of a session closes (i.e. before 10:15) the answer is *yesterday's* stub —
    which is correct, and is exactly what lets a 15:15→15:30 signal be entered
    at the next 09:15 open, as the backtest does.

    Returns None only when no session has completed a bin yet (empty history).
    """
    from src.shared.market_calendar import is_trading_day

    ist_now = now.astimezone(IST)
    idx = bin_index(ist_now)
    if idx >= 1 and ist_now.time() <= SESSION_CLOSE:
        return bar_key(ist_now.date(), idx - 1)
    if ist_now.time() > SESSION_CLOSE:
        return bar_key(ist_now.date(), _BINS_PER_SESSION - 1)
    # Pre-open (or before 10:15 on a session that has not closed a bin yet):
    # the newest complete bin is the PREVIOUS TRADING DAY's stub. Naming the
    # previous *calendar* day instead meant that on a Monday the scan asked for
    # Sunday#6 — a bin that cannot exist — so a Friday 15:15→15:30 signal could
    # never be entered at Monday's 09:15 open. That is the entry the backtest
    # takes 3 times in 214 trades, and it is the ONLY path that reaches the
    # stub bin at all: the last scan of a session runs at 15:16, before the
    # stub has closed.
    prev = ist_now.date() - timedelta(days=1)
    for _ in range(_MAX_MARKET_GAP_DAYS):
        if is_trading_day(prev):
            break
        prev -= timedelta(days=1)
    return bar_key(prev, _BINS_PER_SESSION - 1)


# ---------------------------------------------------------------------------
# Series construction
# ---------------------------------------------------------------------------
def resample_1h(bars: list[OHLCVBar]) -> pd.DataFrame:
    """Aggregate 15m bars into 1h bins anchored per IST day to 09:15.

    Mirrors ``scanner_engine._resample_intraday``: bin = minutes-since-open // 60
    within the IST day, one groupby, OHLCV aggregated first/max/min/last/sum.
    Returns columns ``ist_date, bin, ts_ist, open, high, low, close, volume``
    sorted chronologically. An empty input yields an empty frame.
    """
    if not bars:
        return pd.DataFrame(
            columns=["ist_date", "bin", "ts_ist", "open", "high", "low", "close", "volume"]
        )
    rows = []
    for b in sorted(bars, key=lambda x: x.timestamp):
        ts = ist_dt(b)
        idx = bin_index(ts)
        if idx < 0:
            continue  # pre-open print (auction/oddity) — not part of any bin
        rows.append(
            {
                "ist_date": ts.date(),
                "bin": idx,
                "ts_ist": ts,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume),
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=["ist_date", "bin", "ts_ist", "open", "high", "low", "close", "volume"]
        )
    df = pd.DataFrame(rows)
    out = (
        df.groupby(["ist_date", "bin"], sort=True)
        .agg(
            ts_ist=("ts_ist", "first"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
    )
    return out.sort_values(["ist_date", "bin"]).reset_index(drop=True)


def locate_bin(h1: pd.DataFrame, want_bar_key: str | None) -> int | None:
    """Row index of the bin ``want_bar_key`` names — NOT "the last row".

    The scanned bin is almost never the newest bin in the frame. A scan fires
    one minute AFTER a bin boundary (10:16, 11:16 … 15:16 IST), and by then
    Dhan's 15m feed already carries a bar stamped ``HH:15`` — the next bin,
    still forming. So the newest bin is systematically ``want_bar_key + 1``.
    Requiring the wanted bin to be last therefore never matched, and every
    symbol on every pass bailed out before the cross test (the whole
    2026-07-27..31 live week logged 0 signals of 94 evaluated, 28 scans out of
    28; see ``bugfix_ref`` below).

    Returns None when the bin is genuinely absent — a stale feed, or a name
    that did not trade in that hour. That, not "is it last", is what keeps a
    hit discovered late (bot restart mid-bin) from being entered at a price the
    backtest never saw: the caller always asks for the bin it is scanning.

    Locating rather than pinning also makes the scan robust to stray bars in
    general — Dhan emitted a lone bar stamped Sat 2026-08-01 14:30 IST for many
    NSE symbols, which under the old rule made the newest bin ``2026-08-01#5``
    and killed the scan for every name.

    bugfix_ref: ``Backtesting Engine/strategies/optimized/
    midcap150_meanrev_1h_swing/MEANREV_1H_BUGFIX_HANDOFF.md`` (+
    ``repro_meanrev_bar_key_bug.py``, which drives THIS module and reproduces
    the dead session journal line-for-line).
    """
    if len(h1) == 0:
        return None
    if want_bar_key is None:
        return len(h1) - 1
    keys = h1["ist_date"].astype(str) + "#" + h1["bin"].astype(str)
    matches = h1.index[keys == want_bar_key]
    if len(matches) == 0:
        return None
    return int(matches[0])


def _through_bin(h1: pd.DataFrame, want_bar_key: str | None) -> pd.DataFrame | None:
    """``h1`` truncated so the scanned bin is the last row, or None if absent.

    Everything after the scanned bin is unborn as far as this decision goes.
    Dropping those rows rather than merely indexing around them is what makes
    the in-progress bin structurally unreadable instead of only-accidentally
    unread — EMA here is ``ewm(adjust=False)``, i.e. strictly causal, so the
    retained values are bit-identical to computing over the full frame.
    """
    idx = locate_bin(h1, want_bar_key)
    if idx is None:
        return None
    return h1.iloc[: idx + 1]


def scales_consistent(
    intraday_bars: list[OHLCVBar],
    daily_bars: list[OHLCVBar],
    tolerance: Decimal,
) -> bool:
    """Do the UNADJUSTED intraday and ADJUSTED daily closes agree in scale?

    Port of the backtest's ``_SymbolData._scales_consistent``: compare each
    session's last intraday close against that day's daily close; require the
    median ratio to sit within ``tolerance`` of 1.0 and its relative spread to
    be no wider. Fewer than 20 overlapping sessions ⇒ True (not enough evidence
    to reject — same bias as the backtest).

    A split or bonus that the vendor applied to the daily series but not to
    intraday would otherwise read as a permanent multi-percent "dislocation".
    """
    if not intraday_bars or not daily_bars:
        return True
    intra: dict[date_type, float] = {}
    for b in sorted(intraday_bars, key=lambda x: x.timestamp):
        intra[ist_date(b)] = float(b.close)
    daily: dict[date_type, float] = {}
    for b in sorted(daily_bars, key=lambda x: x.timestamp):
        daily[ist_date(b)] = float(b.close)

    ratios = [
        intra[d] / daily[d]
        for d in intra.keys() & daily.keys()
        if daily.get(d)
    ]
    if len(ratios) < _SCALE_MIN_OVERLAP:
        return True
    s = pd.Series(ratios)
    med = float(s.median())
    if med <= 0:
        return False
    tol = float(tolerance)
    return abs(med - 1.0) <= tol and float(s.std()) / med <= tol


def daily_atr(
    daily_bars: list[OHLCVBar], as_of: date_type, period: int
) -> Decimal | None:
    """ATR(``period``) as of the last DAILY close strictly before ``as_of``.

    Dropping bars dated ``as_of`` or later is deliberate on two counts: it is
    the no-look-ahead definition the backtest uses ("ATR as of the prior daily
    close"), and it sidesteps Dhan's late-publication of the official daily
    candle (PHASES 2026-07-14) — a half-formed today bar can never reach the
    stop distance.
    """
    settled = sorted(
        (b for b in daily_bars if ist_date(b) < as_of), key=lambda b: b.timestamp
    )
    if len(settled) < period + 2:
        return None
    value = ind.atr(ind.bars_to_df(settled), period).iloc[-1]
    if value != value or value <= 0:  # NaN-safe
        return None
    return Decimal(str(round(float(value), 4)))


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------
def evaluate(
    symbol: str,
    intraday_bars: list[OHLCVBar],
    daily_bars: list[OHLCVBar],
    cfg: MeanRevConfig,
    *,
    want_bar_key: str | None = None,
) -> MeanRevSignal | None:
    """Fresh downward cross of the −threshold band on the last complete 1h bin.

    ``want_bar_key`` names which bin is the signal bar (the scan passes the bin
    it is scanning for) and the series is read up to exactly that bin. When the
    bin is absent — a stale feed, a name that did not trade that hour — the
    symbol is skipped rather than entered on an old signal, mirroring the
    gap-reversal path's "actionable only while it is the most recently closed
    bar" rule. Passing None reads the newest bin in the frame, which is only
    ever right for a historical replay.

    Returns None when there is no cross, not enough history, or the scale guard
    rejects the symbol. Thin wrapper over :func:`evaluate_with_reason` — the
    logic lives there so the scan can count WHY each symbol was dropped. This
    signature is what the strategy, the parity script and the dry-run tooling
    call, and is unchanged.
    """
    return evaluate_with_reason(
        symbol, intraday_bars, daily_bars, cfg, want_bar_key=want_bar_key
    ).signal


def evaluate_with_reason(
    symbol: str,
    intraday_bars: list[OHLCVBar],
    daily_bars: list[OHLCVBar],
    cfg: MeanRevConfig,
    *,
    want_bar_key: str | None = None,
) -> MeanRevOutcome:
    """:func:`evaluate` plus WHY, and the numbers computed along the way.

    ``metrics`` accumulates as the evaluation proceeds, so a symbol dropped at
    the cross test still reports the dislocation it actually had. That is what
    makes "the deepest dislocation this bin was −2.1%" answerable from stored
    data instead of a re-fetch of a market that has since moved on.
    """
    metrics: dict[str, str] = {}

    if not scales_consistent(intraday_bars, daily_bars, cfg.scale_tolerance):
        return MeanRevOutcome(None, REASON_SCALE_REJECT, metrics)

    full = resample_1h(intraday_bars)
    metrics["bins_in_frame"] = str(len(full))
    # Pin the scan to the bin it asked for — which is NOT the newest bin in the
    # frame while a bar is still forming. See ``locate_bin``.
    h1 = _through_bin(full, want_bar_key)
    if h1 is None:
        return MeanRevOutcome(None, REASON_BIN_ABSENT, metrics)

    # EMA(20) needs a warm series; the backtest ran it over years of history.
    # 5× the span is where the seeding error falls under a tenth of a percent.
    # Measured up to the SCANNED bin, since that is where the mean is read.
    metrics["bins_available"] = str(len(h1))
    if len(h1) < cfg.ema_len * 5:
        return MeanRevOutcome(None, REASON_COLD_EMA, metrics)

    closes = h1["close"]
    ema = ind.ema(closes, cfg.ema_len)
    dist = (closes / ema - 1.0) * 100.0

    last = len(h1) - 1
    key = bar_key(h1["ist_date"].iloc[last], int(h1["bin"].iloc[last]))

    threshold = -float(cfg.dist_threshold)
    now_dist, prev_dist = float(dist.iloc[last]), float(dist.iloc[last - 1])
    metrics.update(
        bar_key=key,
        close=str(round(float(closes.iloc[last]), 4)),
        ema20=str(round(float(ema.iloc[last]), 4)),
        dist_pct=str(round(now_dist, 4)),
        prev_dist_pct=str(round(prev_dist, 4)),
    )
    if not (now_dist <= threshold and prev_dist > threshold):
        return MeanRevOutcome(None, REASON_NO_CROSS, metrics)

    signal_day: date_type = h1["ist_date"].iloc[last]
    atr14 = daily_atr(daily_bars, signal_day, cfg.atr_period)
    if atr14 is None:
        return MeanRevOutcome(None, REASON_NO_DAILY_ATR, metrics)
    metrics["atr14"] = str(atr14)

    ts_ist: datetime = h1["ts_ist"].iloc[last]
    signal = MeanRevSignal(
        symbol=symbol,
        bar_key=key,
        bar_close_utc=(ts_ist + timedelta(minutes=_BIN_MINUTES)).astimezone(
            UTC
        ),
        close=Decimal(str(round(float(closes.iloc[last]), 4))),
        ema20=Decimal(str(round(float(ema.iloc[last]), 4))),
        dist_pct=Decimal(str(round(now_dist, 4))),
        atr14=atr14,
        stop_distance=(atr14 * cfg.stop_atr_mult),
    )
    metrics["stop_distance"] = str(signal.stop_distance)
    return MeanRevOutcome(signal, REASON_OK, metrics)


def mean_touched(
    intraday_bars: list[OHLCVBar],
    ema_len: int,
    *,
    want_bar_key: str | None = None,
) -> tuple[bool, Decimal, Decimal] | None:
    """Has the bin ``want_bar_key`` names closed back at/above its EMA20?

    Returns ``(touched, close, ema)``, or None when the series is too short or
    that bin is absent. This is the strategy's primary exit — evaluated on
    completed bins only, so an in-progress bar that pokes above the mean and
    falls back never triggers a close the backtest would not have taken.

    That promise used to be docstring-only: this read ``iloc[-1]`` with no
    pinning at all, so at HH:16 it evaluated the bin still forming. Swept over
    the 2026-07-27..31 live week that flipped **161** exit decisions in both
    directions — each a premature or a missed ``mean_touch``. It cost nothing
    only because the entry bug (see :func:`locate_bin`) meant no position was
    ever open; fixing that one alone would have activated this one.

    Callers on a live feed MUST pass ``want_bar_key``. None reads the newest
    bin in the frame, which is only ever right for a historical replay.
    """
    h1 = _through_bin(resample_1h(intraday_bars), want_bar_key)
    if h1 is None or len(h1) < ema_len * 5:
        return None
    closes = h1["close"]
    ema = ind.ema(closes, ema_len)
    close_v = Decimal(str(round(float(closes.iloc[-1]), 4)))
    ema_v = Decimal(str(round(float(ema.iloc[-1]), 4)))
    return (close_v >= ema_v, close_v, ema_v)


def rank_top(signals: list[MeanRevSignal], universe_size: int) -> list[MeanRevSignal]:
    """Deepest dislocation first, then symbol for a deterministic tie-break."""
    ordered = sorted(signals, key=lambda s: (s.dist_pct, s.symbol))
    return ordered[:universe_size]
