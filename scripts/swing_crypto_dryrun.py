"""
Dry-run "deploy" check for the swing-crypto bucket.

Purpose: prove that the freshly added ``ema_9_15`` strategy, its CSV
registration, the bucket configs (scanner / regime / allocator), and the
strategy discovery glue all work together — *without* talking to a live
broker, Postgres, or Binance.

What this script does:
    1. Load the swing-crypto bucket from ``buckets.yaml``.
    2. Validate ``strategy_master.csv``, ``scanner.yaml``,
       ``regime.yaml``, ``allocator.yaml`` parse cleanly.
    3. Discover the strategy file(s) under ``strategies/``.
    4. Synthesise OHLCV bars that contain an EMA-9/15 cross-up on the
       last bar.
    5. Run ``select_entries`` against the synthetic universe.
    6. For each entry, compute the Kelly-derived weight + notional
       *inline* (the sizer's DB-bound version is skipped on purpose).
    7. Print a human-readable "would have done X" report.

Usage:
    PYTHONPATH=. python scripts/swing_crypto_dryrun.py
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from src.core.models import MarketRegime, SizingDecision
from src.data_sources.base import FundingRate, MarketData, OHLCVBar, Ticker
from src.shared.allocator.kelly import fractional_kelly, kelly_fraction
from src.shared.allocator.sizer import load_allocator_config
from src.shared.bucket import load_bucket
from src.shared.regime.brain import load_regime_config
from src.shared.scanner.engine import load_scanner_config
from src.shared.strategy_loader import discover_strategies
from src.shared.strategy_master.loader import load_strategy_master

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fake MarketData
# ---------------------------------------------------------------------------
@dataclass
class FakeMarketData(MarketData):
    bars: dict[str, list[OHLCVBar]]
    last_prices: dict[str, Decimal]

    def get_ohlcv(self, symbol: str, interval: str, limit: int = 500):
        return self.bars.get(symbol, [])[-limit:]

    def get_ticker(self, symbol: str) -> Ticker:
        price = self.last_prices.get(symbol, Decimal("0"))
        return Ticker(
            symbol=symbol,
            last_price=price,
            mark_price=price,
            volume_24h=Decimal("1000000000"),
        )

    def get_tickers(self) -> list[Ticker]:
        return [self.get_ticker(s) for s in self.bars]

    def get_funding_rate(self, symbol: str) -> FundingRate:  # pragma: no cover
        return FundingRate(symbol=symbol, rate=Decimal("0"))


def _bars_with_cross_up(last_price: float) -> list[OHLCVBar]:
    """29 flat bars at last_price/2, then a spike to last_price."""
    closes = [last_price / 2.0] * 29 + [last_price]
    base = datetime(2026, 6, 10, tzinfo=timezone.utc)
    out: list[OHLCVBar] = []
    for i, c in enumerate(closes):
        out.append(
            OHLCVBar(
                timestamp=base + timedelta(hours=i),
                open=Decimal(str(c)),
                high=Decimal(str(c)),
                low=Decimal(str(c)),
                close=Decimal(str(c)),
                volume=Decimal("1000000"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Strategy import (filesystem-based per the runtime convention)
# ---------------------------------------------------------------------------
def _import_strategy_module(folder: Path, name: str):
    path = folder / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_dry_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 70)
    print("swing-crypto dry-run deploy")
    print("=" * 70)

    # Step 1 — load bucket
    bucket = load_bucket("swing-crypto")
    print(f"[ok] bucket loaded: {bucket.id}")
    print(
        f"     capital_inr={bucket.config.capital_inr} "
        f"broker={bucket.config.broker.value} "
        f"leverage_max={bucket.config.leverage_max} "
        f"enabled={bucket.config.enabled}"
    )

    # Step 2 — load all configs
    master = load_strategy_master(
        bucket.strategy_master_csv_path,
        bucket_trading_type=bucket.trading_type.value,
    )
    print(f"[ok] strategy_master.csv: {len(master.rows)} row(s)")
    for r in master.rows:
        gate = sorted(g.value for g in r.allowed_regimes) or ["(any)"]
        print(
            f"     - {r.strategy_name} tf={r.tf} min_vol={r.min_vol} gate={gate}"
        )

    scanner_cfg = load_scanner_config(bucket.scanner_yaml_path)
    print(
        f"[ok] scanner.yaml: universe_size={scanner_cfg.universe_size} "
        f"filters={[f.name for f in scanner_cfg.filters]} "
        f"ranker={scanner_cfg.ranker.name}"
    )

    regime_cfg = load_regime_config(bucket.regime_yaml_path)
    print(
        f"[ok] regime.yaml: enabled={regime_cfg.enabled} "
        f"tf={regime_cfg.tf} proxy={regime_cfg.proxy_symbol} "
        f"cadence={regime_cfg.retrain_cadence}"
    )

    alloc_cfg = load_allocator_config(bucket.allocator_yaml_path)
    print(
        f"[ok] allocator.yaml: fractional_kelly={alloc_cfg.fractional_kelly} "
        f"per_symbol_cap={alloc_cfg.per_symbol_cap} "
        f"aggregate_cap={alloc_cfg.aggregate_cap} "
        f"stats_count={len(alloc_cfg.stats)}"
    )

    # Step 3 — discover strategies
    discovered = discover_strategies(bucket.strategies_folder)
    print(f"[ok] strategies discovered: {list(discovered.keys())}")

    if "ema_9_15" not in discovered:
        print("[FAIL] ema_9_15 not discovered — aborting", file=sys.stderr)
        return 2

    # Step 4 — synthesise universe + market data
    universe_prices = {
        "BTCUSD": Decimal("70000"),
        "ETHUSD": Decimal("3500"),
        "SOLUSD": Decimal("180"),
    }
    bars = {sym: _bars_with_cross_up(float(p)) for sym, p in universe_prices.items()}
    data = FakeMarketData(bars=bars, last_prices=universe_prices)

    # Step 5 — run the strategy
    strat_cls = discovered["ema_9_15"]
    strat = strat_cls()
    entries = strat.select_entries(list(universe_prices.keys()), data)
    print(f"[ok] strategy fired on {len(entries)} symbol(s)")
    for e in entries:
        print(f"     - {e.symbol} side={e.side} hint={e.hint}")

    if not entries:
        print(
            "[FAIL] strategy returned no entries — expected one per universe symbol",
            file=sys.stderr,
        )
        return 3

    # Step 6 — manual Kelly sizing for each entry (skip DB-bound sizer)
    print("\nKelly sizing (manual, no DB) — assumes regime=BULL")
    print(
        f"  capital_inr        = {bucket.config.capital_inr}"
    )
    print(
        f"  leverage_max       = {bucket.config.leverage_max}"
    )
    print(
        f"  fractional_kelly   = {alloc_cfg.fractional_kelly}"
    )
    regime_mult = alloc_cfg.regime_multipliers.get(MarketRegime.BULL, Decimal("1"))
    print(f"  regime_multiplier  = {regime_mult} (bull)")

    print()
    print(
        f"  {'symbol':<10} {'mu':>10} {'sigma':>10} {'k_full':>10} "
        f"{'k_used':>10} {'weight':>8} {'margin':>11} {'notional':>13} {'decision'}"
    )

    available = Decimal(bucket.config.capital_inr)
    total_weight = Decimal("0")
    total_margin = Decimal("0")
    placed = 0
    skipped: dict[SizingDecision, int] = {}

    for e in entries:
        stats = alloc_cfg.stats.get(e.symbol) or alloc_cfg.default_for_unknown
        if stats is None:
            print(f"  {e.symbol:<10} (no stats)")
            skipped[SizingDecision.SKIPPED_OTHER] = (
                skipped.get(SizingDecision.SKIPPED_OTHER, 0) + 1
            )
            continue

        k_full = kelly_fraction(stats.mu_per_period, stats.sigma_per_period)
        k_used = fractional_kelly(k_full, alloc_cfg.fractional_kelly)
        raw_w = k_used * regime_mult
        capped = min(raw_w, alloc_cfg.per_symbol_cap)
        margin = bucket.config.capital_inr * capped
        notional = margin * bucket.config.leverage_max

        if capped <= 0:
            decision = SizingDecision.SKIPPED_NEGATIVE_EDGE
        elif margin > available:
            decision = SizingDecision.SKIPPED_INSUFFICIENT
        else:
            decision = SizingDecision.PLACED
            placed += 1
            total_weight += capped
            total_margin += margin

        skipped[decision] = skipped.get(decision, 0) + 1
        print(
            f"  {e.symbol:<10} {float(stats.mu_per_period):>10.6f} "
            f"{float(stats.sigma_per_period):>10.6f} {float(k_full):>10.4f} "
            f"{float(k_used):>10.4f} {float(capped):>8.4f} "
            f"{float(margin):>11.2f} {float(notional):>13.2f} {decision.value}"
        )

    print()
    print(
        f"summary: placed={placed}, weight_sum={float(total_weight):.4f}, "
        f"margin_used={float(total_margin):.2f}, "
        f"notional={float(total_margin * bucket.config.leverage_max):.2f}"
    )
    if total_weight > alloc_cfg.aggregate_cap:
        print(
            "[note] sum of weights exceeded aggregate_cap; the live sizer "
            "scales them down proportionally"
        )

    # Step 7 — verdict
    print()
    if placed > 0:
        print("[PASS] swing-crypto dry-run produced a non-empty trade plan")
        return 0
    print("[FAIL] no orders would be placed", file=sys.stderr)
    return 4


if __name__ == "__main__":
    sys.exit(main())
