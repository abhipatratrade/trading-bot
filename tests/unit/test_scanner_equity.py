"""Equity scan logic — config parse, daily pass, intraday confirm, ranking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from src.data_sources.base import OHLCVBar
from src.shared.scanner import equity
from src.shared.scanner.engine import load_scanner_config

# Decision 032 moved Blasting Momentum off the bucket's DEFAULT scanner set
# (scanner.yaml is now the 1h mean-reversion config); its own config lives on
# under this name so the equity_daily engine stays covered.
_SCANNER_YAML = Path("src/strategies/swing/indian/scanner_blasting.yaml")


def _cfg() -> equity.EquityScanConfig:
    return equity.EquityScanConfig.from_scanner_config(
        load_scanner_config(_SCANNER_YAML)
    )


def _daily(increments: list[float]) -> list[OHLCVBar]:
    bars: list[OHLCVBar] = []
    v = 100.0
    for i, inc in enumerate(increments):
        v += inc
        bars.append(
            OHLCVBar(
                timestamp=datetime(2026, 7, 1, tzinfo=UTC) + timedelta(days=i),
                open=Decimal(str(round(v - 0.2, 2))),
                high=Decimal(str(round(v + 0.4, 2))),
                low=Decimal(str(round(v - 0.5, 2))),
                close=Decimal(str(round(v, 2))),
                volume=Decimal("100000"),
            )
        )
    return bars


# A near-flat base then a 5-bar burst → RSI≈97 rising, CCI≈216, EMA stacked.
_PASSING = [(0.15 if i % 3 else -0.1) for i in range(25)] + [1.2, 1.6, 2.0, 2.4, 2.8]


def test_blasting_scanner_uses_equity_engine() -> None:
    cfg = load_scanner_config(_SCANNER_YAML)
    assert cfg.engine == "equity_daily"


def test_crypto_scanner_defaults_to_generic_engine() -> None:
    # A crypto bucket config must keep the default generic path (no regression).
    crypto = Path("src/strategies/longterm/crypto/scanner.yaml")
    if crypto.exists():
        assert load_scanner_config(crypto).engine == "generic"


def test_config_parses_scanner_yaml() -> None:
    c = _cfg()
    assert c.universe_size == 5
    assert c.gap_min_pct == Decimal("2.0")
    assert c.rsi_min == Decimal("65.0")
    assert (c.ema_fast, c.ema_slow) == (10, 20)
    assert c.cci_min == Decimal("200.0")
    assert c.price_lo == Decimal("100.0") and c.price_hi == Decimal("2000.0")
    assert c.turnover_min == Decimal("500000")


def test_daily_pass_accepts_momentum_name() -> None:
    s = equity.daily_pass("MOM", _daily(_PASSING), _cfg())
    assert s is not None
    assert s.symbol == "MOM"
    assert s.rsi >= Decimal("65")
    assert s.cci >= Decimal("200")


def test_daily_pass_rejects_flat() -> None:
    assert equity.daily_pass("FLAT", _daily([0.0] * 40), _cfg()) is None


def test_daily_pass_rejects_too_few_bars() -> None:
    assert equity.daily_pass("SHORT", _daily([1.0] * 10), _cfg()) is None


def test_daily_pass_rejects_out_of_price_band() -> None:
    # Same momentum shape but scaled so prev_close < ₹100 floor.
    lowincs = [(0.015 if i % 3 else -0.01) for i in range(25)] + [0.1] * 5
    bars = []
    v = 5.0
    for i, inc in enumerate(lowincs):
        v += inc
        bars.append(
            OHLCVBar(
                timestamp=datetime(2026, 7, 1, tzinfo=UTC) + timedelta(days=i),
                open=Decimal(str(round(v, 3))), high=Decimal(str(round(v + 0.05, 3))),
                low=Decimal(str(round(v - 0.05, 3))), close=Decimal(str(round(v, 3))),
                volume=Decimal("100000"),
            )
        )
    assert equity.daily_pass("CHEAP", bars, _cfg()) is None


def _intraday(open_at_0945: float, vols: tuple[float, float]) -> list[OHLCVBar]:
    """15m bars at 09:15, 09:30 (before) and 09:45 IST (= 03:45/04:00/04:15 UTC)."""
    def bar(utc_h: int, utc_m: int, o: float, vol: float) -> OHLCVBar:
        return OHLCVBar(
            timestamp=datetime(2026, 7, 10, utc_h, utc_m, tzinfo=UTC),
            open=Decimal(str(o)), high=Decimal(str(o + 1)),
            low=Decimal(str(o - 1)), close=Decimal(str(o)),
            volume=Decimal(str(vol)),
        )
    return [
        bar(3, 45, 112.0, vols[0]),   # 09:15 IST
        bar(4, 0, 113.0, vols[1]),    # 09:30 IST
        bar(4, 15, open_at_0945, 0),  # 09:45 IST — its open is the entry price
    ]


def _survivor() -> equity.Survivor:
    return equity.Survivor(
        symbol="MOM", prev_close=Decimal("111.5"),
        rsi=Decimal("97"), cci=Decimal("216"), supertrend=Decimal("106.8"),
    )


def test_intraday_confirm_accepts_gap_up() -> None:
    # 09:45 open 115 vs prev_close 111.5 → +3.1% gap; vols sum 20000.
    c = equity.intraday_confirm(_survivor(), _intraday(115.0, (10000, 10000)), _cfg())
    assert c is not None
    assert c.gap_pct > Decimal("2")
    assert c.cum_volume == Decimal("20000")
    assert c.price == Decimal("115.0")


def test_intraday_confirm_rejects_small_gap() -> None:
    # 09:45 open 112 vs 111.5 → +0.45% gap < 2%.
    assert equity.intraday_confirm(
        _survivor(), _intraday(112.0, (10000, 10000)), _cfg()
    ) is None


def test_intraday_confirm_rejects_thin_volume() -> None:
    # Good gap but only 5,000 shares (< 20,000 floor).
    assert equity.intraday_confirm(
        _survivor(), _intraday(115.0, (2000, 3000)), _cfg()
    ) is None


def test_rank_top_orders_by_gap_desc_and_truncates() -> None:
    def cand(sym: str, gap: float) -> equity.Candidate:
        return equity.Candidate(
            symbol=sym, prev_close=Decimal("100"), price=Decimal("100"),
            gap_pct=Decimal(str(gap)), cum_volume=Decimal("50000"),
        )
    ranked = equity.rank_top(
        [cand("A", 2.5), cand("B", 9.0), cand("C", 4.0)], universe_size=2
    )
    assert [c.symbol for c in ranked] == ["B", "C"]
