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
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

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

# Equity shortlist staleness (days): the daily prepare runs the evening before,
# so the morning scan reads a 1-day-old shortlist normally, up to ~3 over a
# weekend (Fri prepare → Mon scan). Beyond this, treat as no shortlist.
_SHORTLIST_MAX_AGE_DAYS = 4


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
    # Which scan engine runs this config. "generic" = the crypto ticker-snapshot
    # path below (default, so every existing crypto config is unchanged);
    # "equity_daily" = the two-phase Dhan equity path (daily-prepare shortlist +
    # intraday confirm), dispatched to ``run_equity_scan``.
    engine: str = "generic"
    # Free-text universe label for equity configs (e.g. nse_bse_all_equities);
    # informational — the equity universe comes from the Dhan data adapter.
    universe: str | None = None


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

    Equity configs (``engine: equity_daily``) take the two-phase Dhan path
    instead — a daily-prepare shortlist (written by ``prepare_job``) plus an
    intraday gap/volume confirm — via ``run_equity_scan``.
    """
    if config.engine == "equity_daily":
        return run_equity_scan(
            bucket_id=bucket_id, data=data, config=config, scan_date=scan_date
        )

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


def run_equity_scan(
    *,
    bucket_id: str,
    data: MarketData,
    config: ScannerConfig,
    scan_date: date_type,
) -> ScanResult:
    """Two-phase equity scan: read the daily-prepare shortlist, confirm intraday.

    The heavy daily indicator pass runs once/day in ``prepare_job`` and lands as
    ``ScannerSnapshot`` survivor rows (``passed=True`` + indicator metrics). Here,
    per tick during the entry window, we read those survivors, confirm the 09:45
    gap/volume on the morning 15m bars, rank by gap %, and persist the top-N to
    ``DailyUniverse``. ``ScannerSnapshot`` is left intact — the prepare job owns
    it, so a per-tick scan never clobbers the day's shortlist.
    """
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    from src.shared.scanner.equity import (
        Candidate,
        EquityScanConfig,
        Survivor,
        intraday_confirm,
        rank_top,
    )

    ist = _tz(_td(hours=5, minutes=30))
    ecfg = EquityScanConfig.from_scanner_config(config)

    with session_scope() as session:
        # The prepare job runs the evening BEFORE (18:00 IST = 12:30 UTC), so its
        # ScannerSnapshot rows carry the prior UTC date. Match the most recent
        # shortlist within a small staleness window rather than == scan_date, or
        # the morning scan (next UTC day) would never find survivors. Bias: a
        # stale-but-present shortlist is fine (same as the interim tool's age≤3);
        # nothing recent → empty (no entries), which is safe.
        latest_date = session.execute(
            select(func.max(ScannerSnapshot.date)).where(
                ScannerSnapshot.strategy_id == bucket_id,
                ScannerSnapshot.passed.is_(True),
                ScannerSnapshot.date <= scan_date,
                ScannerSnapshot.date >= scan_date - timedelta(days=_SHORTLIST_MAX_AGE_DAYS),
            )
        ).scalar_one_or_none()
        rows = (
            session.execute(
                select(ScannerSnapshot).where(
                    ScannerSnapshot.date == latest_date,
                    ScannerSnapshot.strategy_id == bucket_id,
                    ScannerSnapshot.passed.is_(True),
                )
            )
            .scalars()
            .all()
            if latest_date is not None
            else []
        )
        survivors = [
            Survivor(
                symbol=r.symbol,
                prev_close=Decimal(str(r.metrics.get("prev_close", "0"))),
                rsi=Decimal(str(r.metrics.get("rsi", "0"))),
                cci=Decimal(str(r.metrics.get("cci", "0"))),
                supertrend=Decimal(str(r.metrics.get("supertrend", "0"))),
            )
            for r in rows
        ]

    if not survivors:
        _log.info("equity_scan_no_survivors", bucket_id=bucket_id, date=str(scan_date))

    candidates: list[Candidate] = []
    for s in survivors:
        try:
            bars = data.get_ohlcv(s.symbol, "15m")
        except Exception:
            _log.warning(
                "equity_intraday_fetch_failed", symbol=s.symbol, exc_info=True
            )
            continue
        # 15m fetch can span several sessions — keep only today's bars so the
        # 09:15→09:45 window isn't polluted by prior days at the same wall-clock.
        today = [b for b in bars if b.timestamp.astimezone(ist).date() == scan_date]
        c = intraday_confirm(s, today, ecfg)
        if c is not None:
            candidates.append(c)

    chosen = rank_top(candidates, ecfg.universe_size)
    chosen_symbols = [c.symbol for c in chosen]
    weight = Decimal("1") / Decimal(str(len(chosen))) if chosen else Decimal("0")

    with session_scope() as session:
        session.execute(
            delete(DailyUniverse).where(
                DailyUniverse.date == scan_date,
                DailyUniverse.strategy_id == bucket_id,
            )
        )
        for rank, c in enumerate(chosen, start=1):
            session.add(
                DailyUniverse(
                    date=scan_date,
                    strategy_id=bucket_id,
                    symbol=c.symbol,
                    rank=rank,
                    target_weight=weight,
                    notes=f"gap={c.gap_pct:.2f}% price={c.price}",
                )
            )
        session.add(
            AuditLog(
                strategy_id=bucket_id,
                event_type=AuditEventType.SCANNER_RUN,
                message=(
                    f"equity scan: {len(chosen)}/{len(survivors)} confirmed, "
                    f"top-{ecfg.universe_size}"
                ),
                payload={
                    "bucket_id": bucket_id,
                    "date": str(scan_date),
                    "universe": chosen_symbols,
                    "survivors": len(survivors),
                    "confirmed": len(candidates),
                },
            )
        )

    _log.info(
        "equity_scan_complete",
        bucket_id=bucket_id,
        date=str(scan_date),
        universe=chosen_symbols,
        survivors=len(survivors),
        confirmed=len(candidates),
    )
    return ScanResult(
        bucket_id=bucket_id,
        date=scan_date,
        universe=chosen_symbols,
        evaluated_count=len(survivors),
    )
