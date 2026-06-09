"""
Generic scanner engine.

Each bucket has a ``scanner.yaml`` declaring filters + a ranker. The engine
loads tickers from the bucket's data source, runs filters in order,
ranks the survivors, and returns the top-N universe.

Filters and rankers are registered by name. Adding a new filter is a
matter of writing one function and registering it; no engine change.

Per Decision 008 (deterministic core), the engine produces the same
output for the same inputs and persists a ``ScannerSnapshot`` per
(date, bucket, symbol) row for forensic replay.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
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
from src.data_sources.base import MarketData, Ticker

_log = get_logger("shared.scanner.engine")


# ---------------------------------------------------------------------------
# scanner.yaml schema
# ---------------------------------------------------------------------------
class FilterSpec(BaseModel):
    name: str  # registry key
    params: dict[str, object] = Field(default_factory=dict)


class RankerSpec(BaseModel):
    name: str
    params: dict[str, object] = Field(default_factory=dict)


class ScannerConfig(BaseModel):
    universe_size: int = Field(ge=1, le=100)
    filters: list[FilterSpec] = Field(default_factory=list)
    ranker: RankerSpec


def load_scanner_config(path: Path) -> ScannerConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ScannerConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Filter / Ranker registries
# ---------------------------------------------------------------------------
FilterFn = Callable[[Ticker, dict[str, object]], tuple[bool, dict[str, str]]]
RankFn = Callable[[Ticker, dict[str, object]], Decimal]

_FILTERS: dict[str, FilterFn] = {}
_RANKERS: dict[str, RankFn] = {}


def register_filter(name: str) -> Callable[[FilterFn], FilterFn]:
    def deco(fn: FilterFn) -> FilterFn:
        _FILTERS[name] = fn
        return fn

    return deco


def register_ranker(name: str) -> Callable[[RankFn], RankFn]:
    def deco(fn: RankFn) -> RankFn:
        _RANKERS[name] = fn
        return fn

    return deco


def get_filter(name: str) -> FilterFn:
    if name not in _FILTERS:
        raise KeyError(f"Unknown scanner filter {name!r}. Registered: {list(_FILTERS)}")
    return _FILTERS[name]


def get_ranker(name: str) -> RankFn:
    if name not in _RANKERS:
        raise KeyError(f"Unknown ranker {name!r}. Registered: {list(_RANKERS)}")
    return _RANKERS[name]


# ---------------------------------------------------------------------------
# Built-in filters & ranker
# ---------------------------------------------------------------------------
@register_filter("min_24h_volume_usd")
def _filter_min_volume(
    ticker: Ticker, params: dict[str, object]
) -> tuple[bool, dict[str, str]]:
    threshold = Decimal(str(params.get("threshold", "0")))
    passed = ticker.volume_24h >= threshold
    return passed, {
        "threshold": str(threshold),
        "volume_24h": str(ticker.volume_24h),
        "passed": str(passed),
    }


@register_ranker("volume_24h_desc")
def _rank_volume_desc(ticker: Ticker, _params: dict[str, object]) -> Decimal:
    return ticker.volume_24h


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScanResult:
    bucket_id: str
    date: date_type
    universe: list[str]
    evaluated_count: int


def run_scan(
    *,
    bucket_id: str,
    data: MarketData,
    config: ScannerConfig,
    scan_date: date_type,
    require_binance_listed: bool = True,
) -> ScanResult:
    """Run the scanner for one bucket on one date.

    For crypto buckets we additionally constrain candidates to symbols
    that exist on both Delta India AND Binance (Decision 004) so we have
    a signal feed for every executed symbol.
    """
    # 1. eligible symbols (joined to symbol mapping)
    with session_scope() as session:
        q = select(SymbolMapping).where(SymbolMapping.listed_on_delta.is_(True))
        if require_binance_listed:
            q = q.where(SymbolMapping.listed_on_binance.is_(True))
        eligible_rows = session.execute(q).scalars().all()
    eligible = {m.delta_symbol for m in eligible_rows if m.delta_symbol}

    if not eligible:
        _log.warning("scanner_no_eligible_symbols", bucket_id=bucket_id)
        return ScanResult(
            bucket_id=bucket_id, date=scan_date, universe=[], evaluated_count=0
        )

    # 2. tickers
    tickers = [t for t in data.get_tickers() if t.symbol in eligible]

    # 3. filter pipeline
    filter_fns = [(spec, get_filter(spec.name)) for spec in config.filters]
    rank_fn = get_ranker(config.ranker.name)
    rank_params = dict(config.ranker.params)

    evaluated: list[dict[str, object]] = []
    for ticker in tickers:
        all_results: dict[str, dict[str, str]] = {}
        passed = True
        for spec, fn in filter_fns:
            ok, info = fn(ticker, spec.params)
            all_results[spec.name] = info
            if not ok:
                passed = False
                break
        score = rank_fn(ticker, rank_params) if passed else Decimal("0")
        evaluated.append(
            {
                "symbol": ticker.symbol,
                "passed": passed,
                "filter_results": all_results,
                "score": score,
                "ticker": ticker,
            }
        )

    # 4. rank + pick top-N
    passed_rows = [e for e in evaluated if e["passed"]]
    passed_rows.sort(key=lambda e: e["score"], reverse=True)  # type: ignore[arg-type,return-value]
    chosen = passed_rows[: config.universe_size]
    chosen_symbols: list[str] = [str(c["symbol"]) for c in chosen]
    weight = (
        Decimal("1") / Decimal(str(len(chosen))) if chosen else Decimal("0")
    )

    # 5. persist snapshot + universe (idempotent re-runs)
    with session_scope() as session:
        session.execute(
            delete(ScannerSnapshot).where(
                ScannerSnapshot.date == scan_date,
                ScannerSnapshot.strategy_id == bucket_id,
            )
        )
        session.execute(
            delete(DailyUniverse).where(
                DailyUniverse.date == scan_date,
                DailyUniverse.strategy_id == bucket_id,
            )
        )
        for entry in evaluated:
            ticker: Ticker = entry["ticker"]  # type: ignore[assignment]
            session.add(
                ScannerSnapshot(
                    date=scan_date,
                    strategy_id=bucket_id,
                    symbol=ticker.symbol,
                    metrics={
                        "volume_24h": str(ticker.volume_24h),
                        "last_price": str(ticker.last_price),
                        "mark_price": str(ticker.mark_price)
                        if ticker.mark_price is not None
                        else None,
                        "funding_rate": str(ticker.funding_rate)
                        if ticker.funding_rate is not None
                        else None,
                    },
                    filter_results=entry["filter_results"],
                    rank_score=entry["score"] if entry["passed"] else None,
                    passed=bool(entry["passed"]),
                )
            )
        for rank, entry in enumerate(chosen, start=1):
            ticker = entry["ticker"]  # type: ignore[assignment]
            session.add(
                DailyUniverse(
                    date=scan_date,
                    strategy_id=bucket_id,
                    symbol=ticker.symbol,
                    rank=rank,
                    target_weight=weight,
                    notes=f"score={entry['score']}",
                )
            )
        session.add(
            AuditLog(
                strategy_id=bucket_id,
                event_type=AuditEventType.SCANNER_RUN,
                message=(
                    f"scanner: {len(chosen)}/{len(evaluated)} passed, "
                    f"top-{config.universe_size} chosen"
                ),
                payload={
                    "bucket_id": bucket_id,
                    "date": str(scan_date),
                    "universe": chosen_symbols,
                    "evaluated": len(evaluated),
                    "passed": len(passed_rows),
                },
            )
        )

    _log.info(
        "scan_complete",
        bucket_id=bucket_id,
        date=str(scan_date),
        universe=chosen_symbols,
        evaluated=len(evaluated),
    )

    return ScanResult(
        bucket_id=bucket_id,
        date=scan_date,
        universe=chosen_symbols,
        evaluated_count=len(evaluated),
    )
