"""
Gap-down reversal scan logic — intraday-indian bucket (Decision 029).

Ported from the Backtesting Engine's ``gap_reversal_scanner._find_candidates``
(frozen config: ``strategies/optimized/nifty100_gap_reversal/``). Holdout-
validated 2026-07-19: 35 trades, +13.3% on margin, PF 1.68 on a frozen fold
containing the Apr-2025 crash.

Shape of the scan, per symbol per morning:

  1. **Gap** — ``(open_0915 / prev_session_close - 1) * 100`` must land in
     [-12%, -3%]. BOTH prices come from the same **unadjusted 5m** series.
  2. **Corporate-action guard** — the same gap recomputed against the
     **adjusted daily** prev-close must agree within 1 percentage point.
     A split/bonus adjusts the daily history but not the intraday series, so
     a real corporate action makes the two disagree wildly and the symbol is
     skipped for the day.
  3. **Range** — the first-15m body ``|open_0915 - close_0930|`` must be at
     least 25% of daily ATR(14). Both sides are carried as *fractions of
     price* (body/prev_close vs atr/close) so the test is scale-invariant,
     exactly as the engine does it.

The reversal-candle entry itself is NOT here — it lives in the strategy
(``gap_down_reversal.select_entries``), because it is a per-tick decision on
the 5m stream rather than a once-a-morning universe cut.

LIVE-vs-BACKTEST NOTE (deliberate, see Decision 029): the engine reads
``gap_1d_pct`` off *today's* daily bar, which Dhan does not publish until well
after the close (the 2026-07-14 STALE-CLOSE bug). Live, today's daily open IS
``open_0915`` from the 5m series, so the guard is reconstructed as
``open_0915 / prev_daily_close`` — same comparison, same intent, but it needs
only the PRIOR daily bar. Every daily read here is therefore taken strictly
before ``scan_date``, which makes this path immune to whether Dhan has
published today's candle yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from datetime import time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from src.shared.scanner import indicators as ind

if TYPE_CHECKING:
    from src.data_sources.base import OHLCVBar
    from src.shared.scanner.engine import ScannerConfig

IST = timezone(timedelta(hours=5, minutes=30))

# The three 5m bars that make up the first 15 minutes. The body is measured
# from the 09:15 bar's OPEN to the 09:25 bar's CLOSE (= 09:30).
_OPEN_BAR = time(9, 15)
_THIRD_BAR = time(9, 25)
# A clean session needs at least this many 5m bars present (engine: len(idx)<6).
_MIN_SESSION_BARS = 6


@dataclass(frozen=True, slots=True)
class GapReversalConfig:
    """Thresholds parsed from the bucket's ``scanner.yaml``."""

    universe_size: int
    symbols: tuple[str, ...]
    gap_min_pct: Decimal
    gap_max_pct: Decimal
    gap_mismatch_pct: Decimal
    first15_body_atr_frac: Decimal
    atr_period: int
    # Minimum hard price-band width for a NON-F&O scrip to be eligible. 0
    # disables the check. F&O underlyings always pass (dynamic band).
    min_circuit_band_pct: Decimal

    @classmethod
    def from_scanner_config(cls, config: ScannerConfig) -> GapReversalConfig:
        """Read thresholds out of the named filters (order-independent)."""
        by_name = {f.name: f.params for f in config.filters}

        def _p(name: str, key: str, default: object) -> Decimal:
            return Decimal(str(by_name.get(name, {}).get(key, default)))

        def _i(name: str, key: str, default: int) -> int:
            return int(by_name.get(name, {}).get(key, default))

        return cls(
            universe_size=config.universe_size,
            symbols=tuple(config.symbols),
            gap_min_pct=_p("gap_down_pct", "min", 3.0),
            gap_max_pct=_p("gap_down_pct", "max", 12.0),
            gap_mismatch_pct=_p("corporate_action_guard", "max_mismatch_pct", 1.0),
            first15_body_atr_frac=_p("first15_body_atr_frac", "threshold", 0.25),
            atr_period=_i("first15_body_atr_frac", "atr_period", 14),
            min_circuit_band_pct=_p("circuit_band_min", "threshold", 0),
        )


@dataclass(frozen=True, slots=True)
class GapCandidate:
    """A symbol that cleared the morning gap/range cut — eligible to be faded."""

    symbol: str
    prev_close: Decimal          # last 5m close of the PRIOR session (unadjusted)
    open_0915: Decimal
    gap_pct: Decimal             # negative (gap DOWN)
    body_atr_ratio: Decimal      # first-15m body ÷ daily ATR, both as price fractions


def ist_time(bar: OHLCVBar) -> time:
    """The bar's wall-clock time in IST (bars are UTC-stamped)."""
    return bar.timestamp.astimezone(IST).time()


def ist_date(bar: OHLCVBar) -> date_type:
    """The bar's IST calendar date."""
    return bar.timestamp.astimezone(IST).date()


def session_bars(bars: list[OHLCVBar], day: date_type) -> list[OHLCVBar]:
    """Bars belonging to ``day``'s IST session, in chronological order."""
    return sorted(
        (b for b in bars if ist_date(b) == day), key=lambda b: b.timestamp
    )


def prev_session_close(bars: list[OHLCVBar], day: date_type) -> Decimal | None:
    """Last 5m close of the most recent session strictly BEFORE ``day``.

    This is the unadjusted reference the gap is measured against — never a
    daily close, which the vendor may have adjusted for a corporate action.
    """
    earlier = [b for b in bars if ist_date(b) < day]
    if not earlier:
        return None
    last = max(earlier, key=lambda b: b.timestamp)
    return Decimal(str(last.close))


def daily_context(
    daily_bars: list[OHLCVBar], day: date_type, atr_period: int
) -> tuple[Decimal, Decimal] | None:
    """``(atr_pct, prev_daily_close)`` as of the last close BEFORE ``day``.

    ``atr_pct`` is ATR(``atr_period``) divided by that same bar's close — a
    fraction of price, so it is unaffected by whether the vendor has rescaled
    the series for a corporate action. ``prev_daily_close`` feeds the
    corporate-action guard in ``gap_screen``.

    Bars dated ``day`` or later are dropped up front, so a late-arriving or
    partially-formed daily candle for today can never leak into the read.
    """
    settled = sorted(
        (b for b in daily_bars if ist_date(b) < day), key=lambda b: b.timestamp
    )
    if len(settled) < atr_period + 2:
        return None
    df = ind.bars_to_df(settled)
    atr_val = ind.atr(df, atr_period).iloc[-1]
    close = df["close"].iloc[-1]
    if not (close > 0) or atr_val != atr_val:  # NaN-safe
        return None
    return (
        Decimal(str(float(atr_val) / float(close))),
        Decimal(str(round(float(close), 4))),
    )


def gap_screen(
    symbol: str,
    intraday_bars: list[OHLCVBar],
    daily_bars: list[OHLCVBar],
    day: date_type,
    cfg: GapReversalConfig,
) -> GapCandidate | None:
    """Apply the morning gap + corporate-action + range cut for one symbol.

    ``intraday_bars`` must be 5m bars spanning at least the prior session and
    today's open; ``daily_bars`` daily bars ending at or before the prior
    session. Returns a ``GapCandidate`` or None.
    """
    today = session_bars(intraday_bars, day)
    if len(today) < _MIN_SESSION_BARS:
        return None
    # A clean open is required: the engine refuses any day whose first bar
    # isn't 09:15, and needs 09:15/09:20/09:25 present to measure the body.
    if ist_time(today[0]) != _OPEN_BAR or ist_time(today[2]) != _THIRD_BAR:
        return None

    prev_close = prev_session_close(intraday_bars, day)
    if prev_close is None or prev_close <= 0:
        return None

    ctx = daily_context(daily_bars, day, cfg.atr_period)
    if ctx is None:
        return None
    atr_pct, prev_daily_close = ctx
    if atr_pct <= 0 or prev_daily_close <= 0:
        return None

    open_0915 = Decimal(str(today[0].open))
    gap_pct = (open_0915 / prev_close - 1) * 100

    # 1. gap DOWN, within the sane band (beyond gap_max it's a circuit/corp action)
    if not (-cfg.gap_max_pct <= gap_pct <= -cfg.gap_min_pct):
        return None

    # 2. corporate-action guard — the same gap, recomputed against the ADJUSTED
    #    daily prev-close, must agree within gap_mismatch_pct. LIVE both series
    #    are on today's scale, so they agree and this passes; they diverge only
    #    once the vendor books an adjustment into the daily history that the
    #    unadjusted intraday series will never receive — precisely the state in
    #    which a measured "gap" is fiction. Skipping is the safe direction.
    #
    #    This is a deliberate reformulation. The engine computes the daily gap
    #    WITHIN the daily series (today's daily open ÷ prev daily close), which
    #    is scale-invariant but needs TODAY's daily bar — and Dhan does not
    #    publish it until well after the close (the 2026-07-14 STALE-CLOSE bug),
    #    so it is unusable at 09:30. Substituting open_0915 for today's daily
    #    open costs that scale-invariance.
    #
    #    REPLAY CAVEAT: because historical daily data is retroactively adjusted,
    #    replaying a date that PRECEDES a corporate action sees the two series on
    #    different scales and skips the name, where live (both current-scale) it
    #    would have traded. Measured across all 76 frozen backtest trades this
    #    costs exactly one: VEDL 2025-08-26, whose daily history is rescaled
    #    ×0.374 by the later Vedanta demerger. The scale-invariant alternative
    #    (comparing consecutive within-series close ratios) was tried and is
    #    worse: the 5m series' last bar closes at 15:25 and misses the closing
    #    auction, so it disagrees with the official daily close by ~1% on
    #    ordinary days — that guard rejected IOC and TORNTPHARM on pure noise.
    gap_1d_pct = (open_0915 / prev_daily_close - 1) * 100
    if abs(gap_pct - gap_1d_pct) > cfg.gap_mismatch_pct:
        return None

    # 3. first-15m body vs daily ATR, both as fractions of their own scale
    close_0930 = Decimal(str(today[2].close))
    body_frac = abs(close_0930 - open_0915) / prev_close
    if body_frac < cfg.first15_body_atr_frac * atr_pct:
        return None

    return GapCandidate(
        symbol=symbol,
        prev_close=prev_close,
        open_0915=open_0915,
        gap_pct=gap_pct,
        body_atr_ratio=body_frac / atr_pct,
    )


def rank_top(
    candidates: list[GapCandidate], universe_size: int
) -> list[GapCandidate]:
    """Rank by |gap| descending, keep the top N.

    The backtest fills chronologically as signals fire and only uses |gap| to
    break ties within the same 5m bar. The scanner can't know fire order ahead
    of time, so it ranks the morning cut by the same tie-break key; the
    chronological part is emergent live (a symbol enters when its candle
    prints).
    """
    return sorted(candidates, key=lambda c: abs(c.gap_pct), reverse=True)[
        :universe_size
    ]
