"""
Volume scanner — rank symbols by 24h notional volume, filter to tradeable universe.

Used by crypto_longterm (Phase 1) and potentially crypto_swing (Phase 2).
Writes results to scanner_snapshot + daily_universe tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import delete, select

from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    DailyUniverse,
    ScannerSnapshot,
    SymbolMapping,
)
from src.data_sources.base import MarketData

_log = get_logger("scanner.volume")


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Returned by run_volume_scan."""

    date: date_type
    strategy_id: str
    universe: list[str]
    all_evaluated: list[dict]


def run_volume_scan(
    *,
    strategy_id: str,
    data_source: MarketData,
    scan_date: date_type,
    max_positions: int,
    min_24h_volume_usd: Decimal = Decimal("0"),
) -> ScanResult:
    """Rank Delta India perps by 24h volume, filter to Binance-listed, pick top N.

    Steps:
      1. Load symbol mappings from DB (listed_on_delta AND listed_on_binance).
      2. Fetch all tickers from the data source (Delta India).
      3. Filter to mapped symbols, apply min volume threshold.
      4. Rank by volume_24h descending, take top max_positions.
      5. Write ScannerSnapshot rows (full audit) and DailyUniverse rows (lean read-side).

    Returns the chosen universe as a list of Delta symbols.
    """

    # 1. Get eligible symbols from mapping table
    with session_scope() as session:
        mappings = session.execute(
            select(SymbolMapping).where(
                SymbolMapping.listed_on_delta.is_(True),
                SymbolMapping.listed_on_binance.is_(True),
            )
        ).scalars().all()
        eligible = {m.delta_symbol: m.canonical_symbol for m in mappings if m.delta_symbol}

    if not eligible:
        _log.warning("no_eligible_symbols", strategy_id=strategy_id)
        return ScanResult(
            date=scan_date, strategy_id=strategy_id, universe=[], all_evaluated=[]
        )

    # 2. Fetch tickers
    tickers = data_source.get_tickers()
    ticker_map = {t.symbol: t for t in tickers}

    # 3. Evaluate each eligible symbol
    evaluated: list[dict] = []
    for delta_sym, canonical in eligible.items():
        ticker = ticker_map.get(delta_sym)
        if ticker is None:
            evaluated.append({
                "symbol": delta_sym,
                "canonical": canonical,
                "volume_24h": Decimal("0"),
                "passed": False,
                "filter_results": {"reason": "no_ticker_data"},
            })
            continue

        vol = ticker.volume_24h
        passed = vol >= min_24h_volume_usd

        evaluated.append({
            "symbol": delta_sym,
            "canonical": canonical,
            "volume_24h": vol,
            "last_price": ticker.last_price,
            "mark_price": ticker.mark_price,
            "funding_rate": ticker.funding_rate,
            "open_interest": ticker.open_interest,
            "passed": passed,
            "filter_results": {
                "min_volume_check": str(vol >= min_24h_volume_usd),
                "volume_24h_usd": str(vol),
                "threshold": str(min_24h_volume_usd),
            },
        })

    # 4. Rank passed symbols by volume, take top N
    passed = [e for e in evaluated if e["passed"]]
    passed.sort(key=lambda x: x["volume_24h"], reverse=True)
    chosen = passed[:max_positions]

    universe_symbols = [c["symbol"] for c in chosen]
    weight = Decimal("1") / Decimal(str(len(chosen))) if chosen else Decimal("0")

    # 5. Persist to DB
    with session_scope() as session:
        # Clear old snapshots for this date+strategy (idempotent re-runs)
        session.execute(
            delete(ScannerSnapshot).where(
                ScannerSnapshot.date == scan_date,
                ScannerSnapshot.strategy_id == strategy_id,
            )
        )
        session.execute(
            delete(DailyUniverse).where(
                DailyUniverse.date == scan_date,
                DailyUniverse.strategy_id == strategy_id,
            )
        )

        for i, entry in enumerate(evaluated):
            rank_score = entry["volume_24h"] if entry["passed"] else Decimal("0")
            session.add(
                ScannerSnapshot(
                    date=scan_date,
                    strategy_id=strategy_id,
                    symbol=entry["symbol"],
                    metrics={
                        "volume_24h_usd": str(entry["volume_24h"]),
                        "last_price": str(entry.get("last_price", "")),
                        "mark_price": str(entry.get("mark_price", "")),
                        "funding_rate": str(entry.get("funding_rate", "")),
                        "open_interest": str(entry.get("open_interest", "")),
                    },
                    filter_results=entry["filter_results"],
                    rank_score=rank_score,
                    passed=entry["passed"],
                )
            )

        for rank, entry in enumerate(chosen, start=1):
            session.add(
                DailyUniverse(
                    date=scan_date,
                    strategy_id=strategy_id,
                    symbol=entry["symbol"],
                    rank=rank,
                    target_weight=weight,
                    notes=f"vol={entry['volume_24h']:.0f} USD",
                )
            )

        session.add(
            AuditLog(
                strategy_id=strategy_id,
                event_type=AuditEventType.SCANNER_RUN,
                message=f"Volume scan: {len(chosen)}/{len(evaluated)} symbols selected",
                payload={
                    "date": str(scan_date),
                    "universe": universe_symbols,
                    "evaluated_count": len(evaluated),
                    "passed_count": len(passed),
                },
            )
        )

    _log.info(
        "volume_scan_complete",
        strategy_id=strategy_id,
        date=str(scan_date),
        universe=universe_symbols,
        evaluated=len(evaluated),
    )

    return ScanResult(
        date=scan_date,
        strategy_id=strategy_id,
        universe=universe_symbols,
        all_evaluated=evaluated,
    )
