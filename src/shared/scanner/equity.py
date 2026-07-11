"""
Equity scan logic — Blasting Momentum spec (Decision 012; Phase 4).

The crypto scanner ranks a live 24h ``Ticker`` snapshot in one pass. The equity
scanner is two-phase and history-based, so it lives here rather than shoe-horned
into the generic ``Ticker`` filter registry:

  * **daily pass** (heavy, once/day in the prepare job): over the whole NSE+BSE
    universe, compute daily indicators as of the prior settled close and keep the
    names that clear RSI(14)≥min & rising, EMA(fast)>EMA(slow), CCI(14)≥min, and
    the price band. Emits a *survivor* row (prev_close + indicator values).
  * **intraday confirm** (light, per tick during the entry window): for each
    survivor, read the morning 15m bars, compute the 09:45 gap vs prior close and
    the 09:15→09:45 cumulative volume, and keep names clearing gap/volume/turnover.
    Rank by gap % and take the top N.

These are pure functions over ``MarketData``; the prepare job (DB shortlist) and
the ``run_scan`` equity branch that persist their output wrap them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from src.shared.scanner import indicators as ind

if TYPE_CHECKING:
    from src.data_sources.base import OHLCVBar
    from src.shared.scanner.engine import ScannerConfig

# NSE session: the entry moment is 09:45 IST (daily indicators as of the prior
# close; gap/volume confirmed on the morning's 15m bars up to this point).
_IST = timezone(timedelta(hours=5, minutes=30))
_ENTRY_CUTOFF = time(9, 45)


@dataclass(frozen=True, slots=True)
class EquityScanConfig:
    """Thresholds parsed from a bucket's equity ``scanner.yaml``."""

    universe_size: int
    gap_min_pct: Decimal
    rsi_period: int
    rsi_min: Decimal
    ema_fast: int
    ema_slow: int
    cci_period: int
    cci_min: Decimal
    cum_volume_min: Decimal
    price_lo: Decimal
    price_hi: Decimal
    turnover_min: Decimal

    @classmethod
    def from_scanner_config(cls, config: ScannerConfig) -> EquityScanConfig:
        """Read thresholds out of the named filters (order-independent)."""
        by_name = {f.name: f.params for f in config.filters}

        def _p(name: str, key: str, default: object) -> Decimal:
            return Decimal(str(by_name.get(name, {}).get(key, default)))

        def _i(name: str, key: str, default: int) -> int:
            return int(by_name.get(name, {}).get(key, default))

        return cls(
            universe_size=config.universe_size,
            gap_min_pct=_p("gap_up_pct_min", "threshold", 2.0),
            rsi_period=_i("daily_rsi_min_rising", "period", 14),
            rsi_min=_p("daily_rsi_min_rising", "threshold", 65.0),
            ema_fast=_i("daily_ema_stack", "fast", 10),
            ema_slow=_i("daily_ema_stack", "slow", 20),
            cci_period=_i("daily_cci_min", "period", 14),
            cci_min=_p("daily_cci_min", "threshold", 200.0),
            cum_volume_min=_p("cum_volume_min", "threshold", 20000),
            price_lo=_p("price_band_inr", "low", 100.0),
            price_hi=_p("price_band_inr", "high", 2000.0),
            turnover_min=_p("turnover_min_inr", "threshold", 500000),
        )


@dataclass(frozen=True, slots=True)
class Survivor:
    """A name that cleared the daily pass (indicator values as of prior close)."""

    symbol: str
    prev_close: Decimal
    rsi: Decimal
    cci: Decimal
    supertrend: Decimal


@dataclass(frozen=True, slots=True)
class Candidate:
    """A survivor that also cleared the intraday confirm at the entry moment."""

    symbol: str
    prev_close: Decimal
    price: Decimal
    gap_pct: Decimal
    cum_volume: Decimal


def daily_pass(
    symbol: str, daily_bars: list[OHLCVBar], cfg: EquityScanConfig
) -> Survivor | None:
    """Apply the prior-close daily filters. Returns a Survivor or None.

    Gap and volume are intraday and NOT checked here — that's ``intraday_confirm``.
    """
    min_bars = max(cfg.ema_slow, cfg.rsi_period, cfg.cci_period, 10) + 5
    if len(daily_bars) < min_bars:
        return None
    df = ind.bars_to_df(daily_bars)
    prev_close = df["close"].iloc[-1]
    if not (float(cfg.price_lo) <= prev_close <= float(cfg.price_hi)):
        return None

    rsi = ind.rsi(df["close"], cfg.rsi_period)
    r0, r1 = float(rsi.iloc[-1]), float(rsi.iloc[-2])
    if not (r0 >= float(cfg.rsi_min) and r0 > r1):  # ≥ threshold AND rising
        return None
    ema_f = ind.ema(df["close"], cfg.ema_fast).iloc[-1]
    ema_s = ind.ema(df["close"], cfg.ema_slow).iloc[-1]
    if not ema_f > ema_s:
        return None
    cci = ind.cci(df, cfg.cci_period).iloc[-1]
    if not float(cci) >= float(cfg.cci_min):
        return None
    st = ind.supertrend(df).iloc[-1]
    return Survivor(
        symbol=symbol,
        prev_close=Decimal(str(round(prev_close, 4))),
        rsi=Decimal(str(round(r0, 2))),
        cci=Decimal(str(round(float(cci), 2))),
        supertrend=Decimal(str(round(float(st), 4))),
    )


def intraday_confirm(
    survivor: Survivor, intraday_bars: list[OHLCVBar], cfg: EquityScanConfig
) -> Candidate | None:
    """Confirm the 09:45 gap + morning volume/turnover on the 15m bars."""
    before = [b for b in intraday_bars if _ist_time(b) < _ENTRY_CUTOFF]
    at = [b for b in intraday_bars if _ist_time(b) >= _ENTRY_CUTOFF]
    if not before:
        return None
    price = Decimal(str(at[0].open if at else before[-1].close))
    cum_vol = sum((Decimal(str(b.volume)) for b in before), Decimal("0"))
    if survivor.prev_close <= 0:
        return None
    gap_pct = (price / survivor.prev_close - 1) * 100
    if not (
        gap_pct >= cfg.gap_min_pct
        and cum_vol >= cfg.cum_volume_min
        and cfg.price_lo <= price <= cfg.price_hi
        and price * cum_vol >= cfg.turnover_min
    ):
        return None
    return Candidate(
        symbol=survivor.symbol,
        prev_close=survivor.prev_close,
        price=price,
        gap_pct=gap_pct,
        cum_volume=cum_vol,
    )


def rank_top(candidates: list[Candidate], universe_size: int) -> list[Candidate]:
    """Rank by gap % descending (``gap_up_pct_desc``), keep the top N."""
    return sorted(candidates, key=lambda c: c.gap_pct, reverse=True)[:universe_size]


def _ist_time(bar: OHLCVBar) -> time:
    """The bar's wall-clock time in IST (bars are UTC-stamped)."""
    return bar.timestamp.astimezone(_IST).time()
