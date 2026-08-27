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

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
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
    OrderStatus,
    ScannerSnapshot,
    SymbolMapping,
    Trade,
)
from src.data_sources.base import MarketData, Ticker

_log = get_logger("shared.scanner.engine")

# Equity shortlist staleness (days): the daily prepare runs the evening before,
# so the morning scan reads a 1-day-old shortlist normally, up to ~3 over a
# weekend (Fri prepare → Mon scan). Beyond this, treat as no shortlist.
_SHORTLIST_MAX_AGE_DAYS = 4

# How many times the gap screen may re-run in a session when every symbol came
# back data-limited. See the retry rationale in run_gap_reversal_scan: high
# enough to ride out a slow feed at the open, low enough that it can never
# become the hour of continuous re-fetching that would trip Dhan's 5 req/s cap.
_MAX_SCREEN_ATTEMPTS = 5


def should_rescreen(
    reasons: list[str], attempts: int, cap: int = _MAX_SCREEN_ATTEMPTS
) -> bool:
    """Should the gap screen run again, given what today's rows already say? PURE.

    True only when rows exist AND every one of them failed on a ``data_``
    reason AND we are under the attempt cap. A single evaluable row means the
    screen genuinely ran — an empty cut is then a real answer about the market
    and must not be re-fetched every tick for the rest of the morning.

    ``reasons`` is one entry per persisted symbol; "" is an evaluable pass
    (the screen got all the way through and the symbol qualified).
    """
    if not reasons or attempts >= cap:
        return False
    return all(r.startswith("data_") for r in reasons)


def _utc_start_of(day: date_type) -> datetime:
    """Midnight UTC on ``day`` — the lower bound for 'attempts made today'.

    The gap screen's ``scan_date`` is already the UTC date the run_bot loop
    passes, and the morning cut happens ~04:00 UTC, so a UTC-day window and the
    IST session it describes never disagree here.
    """
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


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
    # intraday confirm), dispatched to ``run_equity_scan``;
    # "equity_intraday" = the morning gap-down cut (Decision 029), dispatched to
    # ``run_gap_reversal_scan``.
    # "equity_meanrev_1h" = the 1h EMA20-dislocation cut (Decision 032),
    # dispatched to ``run_meanrev_scan``.
    engine: str = "generic"
    # Free-text universe label for equity configs (e.g. nse_bse_all_equities);
    # informational — the equity universe comes from the Dhan data adapter.
    universe: str | None = None
    # Explicit symbol universe. Used by ``equity_intraday``, whose universe is a
    # fixed index membership (NIFTY-100) rather than a broker-wide sweep — the
    # constituents live in git so the scanned set is auditable (House Rule 7).
    symbols: list[str] = Field(default_factory=list)


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
    now: datetime | None = None,
) -> ScanResult:
    """Run the scanner for one bucket on one date.

    For crypto buckets we additionally constrain candidates to symbols
    that exist on both Delta India AND Binance (Decision 004) so we have
    a signal feed for every executed symbol.

    Equity configs (``engine: equity_daily``) take the two-phase Dhan path
    instead — a daily-prepare shortlist (written by ``prepare_job``) plus an
    intraday gap/volume confirm — via ``run_equity_scan``.

    ``now`` is the tick's clock reading; only the 1h mean-reversion path needs
    it (to know which bar it is scanning for). None ⇒ wall clock.
    """
    if config.engine == "equity_daily":
        return run_equity_scan(
            bucket_id=bucket_id, data=data, config=config, scan_date=scan_date
        )
    if config.engine == "equity_intraday":
        return run_gap_reversal_scan(
            bucket_id=bucket_id, data=data, config=config, scan_date=scan_date
        )
    if config.engine == "equity_meanrev_1h":
        return run_meanrev_scan(
            bucket_id=bucket_id,
            data=data,
            config=config,
            scan_date=scan_date,
            now=now or datetime.now(UTC),
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
        # A once-a-day screen: the bar it is about IS the day (see
        # ``models._BAR_KEY_DOC``), so the delete above stays day-scoped.
        day_bar = scan_date.isoformat()
        for entry in evaluated:
            ticker: Ticker = entry["ticker"]  # type: ignore[assignment]
            session.add(
                ScannerSnapshot(
                    date=scan_date,
                    strategy_id=bucket_id,
                    symbol=ticker.symbol,
                    bar_key=day_bar,
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
                    bar_key=day_bar,
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
                    # Re-run per tick inside the entry window, but always about
                    # the same bar: today's 09:15→09:45 gap. Day-scoped.
                    bar_key=scan_date.isoformat(),
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


def entries_taken_today(bucket_id: str, day: date_type) -> int:
    """How many NEW positions this bucket has opened during ``day`` (IST).

    Counts non-reduce-only, non-rejected ``Trade`` rows — the portfolio rule
    "max N new entries per day" is a property of the book, not of one strategy,
    so it is scoped to the bucket. Exits and protective stops are reduce-only
    and never count.
    """
    from src.shared.scanner.meanrev import IST

    start = datetime.combine(day, datetime.min.time(), tzinfo=IST)
    rows = 0
    with session_scope() as session:
        trades = (
            session.execute(
                select(Trade).where(
                    Trade.bucket_id == bucket_id,
                    Trade.created_at >= start,
                    Trade.created_at < start + timedelta(days=1),
                    Trade.status.in_(
                        [
                            OrderStatus.PENDING,
                            OrderStatus.OPEN,
                            OrderStatus.PARTIAL,
                            OrderStatus.FILLED,
                        ]
                    ),
                )
            )
            .scalars()
            .all()
        )
        for t in trades:
            if not (t.extra or {}).get("reduce_only"):
                rows += 1
    return rows


# In-process cache: bucket_id → (bar_key, ScanResult). The 1h signal cannot
# change inside a bin, so a 60s tick loop must not re-fetch 94 symbols × 2
# series every minute. A restart simply re-scans the current bin.
_MEANREV_SCAN_CACHE: dict[str, tuple[str, ScanResult]] = {}

# bucket_id → (bar_key, attempts, last_attempt) for a bin whose scan came back
# with NO usable data. Such a bin is re-fetched inside its own window instead
# of being cached as settled — see :func:`_blind_retry_due`.
_MEANREV_BLIND_ATTEMPTS: dict[str, tuple[str, int, datetime]] = {}

# A blind bin is re-fetched at most this many times, this far apart. Six tries
# five minutes apart walk a 09:16 stub-bin scan out to ~09:41 — still well
# inside the bin, and ~1,100 Dhan calls worst case, which the 5 req/s pacer
# absorbs. Past that the data is genuinely not coming, and the SCANNER
# DEGRADED notice is then the correct and wanted output.
_BLIND_RETRY_MAX = 6
_BLIND_RETRY_GAP = timedelta(minutes=5)


def _blind_retry_due(bucket_id: str, key: str, now: datetime) -> bool:
    """True when a bin that scanned blind has earned another fetch.

    The cache is keyed on the bar key alone, so a pass that could evaluate
    nothing used to pin that verdict for the whole bin. The 09:16 scan — the
    ONLY one that ever reads the previous session's 15:15→15:30 stub — thus
    got exactly one attempt, one minute after the open, and Dhan often has not
    published the prior session's final 15m bar by then. Every symbol came
    back ``data_bin_absent``, that result was cached, and the stub was never
    looked at again before the bin rolled at 10:16.

    Two things followed. The stub entry the backtest takes (3 of 214 trades)
    almost never fired live, and the morning pass cried SCANNER DEGRADED on 8
    of 10 sessions — training the reader to ignore the one check that watches
    perception. On 2026-08-07 the single attempt happened to land after Dhan
    had published, and all 94 symbols resolved the stub normally, which is the
    proof that the bar is real and merely late.
    """
    seen_key, attempts, last = _MEANREV_BLIND_ATTEMPTS.get(bucket_id, ("", 0, now))
    if seen_key != key or attempts >= _BLIND_RETRY_MAX:
        return False
    return (now - last) >= _BLIND_RETRY_GAP


def run_meanrev_scan(
    *,
    bucket_id: str,
    data: MarketData,
    config: ScannerConfig,
    scan_date: date_type,
    now: datetime,
) -> ScanResult:
    """1h EMA20-dislocation cut for the swing-indian bucket (Decision 032).

    Runs ONCE per completed 1h bin and caches the result: the inputs are fixed
    the moment the bin closes, so re-screening every 60s would burn ~190 Dhan
    calls a minute recomputing a constant.

    Unlike the gap-reversal cut this is not a once-a-day screen — the universe
    is re-derived on every bin (10:15, 11:15 … 15:30), and the day's remaining
    entry budget shrinks it as positions are opened.
    """
    from src.shared.scanner.meanrev import (
        MeanRevConfig,
        MeanRevOutcome,
        MeanRevSignal,
        evaluate_with_reason,
        last_complete_bar_key,
        rank_top,
    )

    cfg = MeanRevConfig.from_scanner_config(config)
    key = last_complete_bar_key(now)
    cached = _MEANREV_SCAN_CACHE.get(bucket_id)
    if (
        cached is not None
        and cached[0] == key
        and not _blind_retry_due(bucket_id, key, now)
    ):
        return cached[1]

    # Named scanner sets namespace their snapshots as "<bucket>:<name>"; the
    # trade ledger is keyed by the bare bucket id.
    ledger_bucket = bucket_id.split(":", 1)[0]

    symbols = list(cfg.symbols)
    if cfg.fno_only and hasattr(data, "universe"):
        universe = data.universe
        eligible = [s for s in symbols if universe.get(s, {}).get("fno") == "1"]
        if len(eligible) != len(symbols):
            _log.info(
                "meanrev_scan_non_fno_excluded",
                bucket_id=bucket_id,
                excluded=sorted(set(symbols) - set(eligible)),
                kept=len(eligible),
                of=len(symbols),
            )
        symbols = eligible

    signals: list[MeanRevSignal] = []
    evaluated: list[tuple[str, MeanRevOutcome]] = []
    for symbol in symbols:
        try:
            intraday = _intraday_history(data, symbol, cfg.intraday_lookback_days)
            daily = data.get_ohlcv(symbol, "1d", limit=cfg.atr_period + 40)
        except Exception:
            # One unfetchable name must never sink the whole bin's scan.
            _log.warning("meanrev_scan_fetch_failed", symbol=symbol, exc_info=True)
            continue
        outcome = evaluate_with_reason(symbol, intraday, daily, cfg, want_bar_key=key)
        evaluated.append((symbol, outcome))
        if outcome.signal is not None:
            signals.append(outcome.signal)

    # Decision 033: "evaluated" used to be incremented BEFORE the cut ran, so
    # it only ever meant "the fetch succeeded". That made "94 names checked,
    # none crossed" and "94 names bailed at the first guard" the same log line
    # — which is precisely how a scanner that was structurally incapable of
    # firing read as a quiet market for a full week. Count the outcomes.
    by_reason = Counter(o.reason or "signal" for _, o in evaluated)
    unevaluable = [sym for sym, o in evaluated if not o.data_ok]
    if unevaluable:
        _log.warning(
            "meanrev_scan_symbols_unevaluable",
            bucket_id=bucket_id,
            bar_key=key,
            count=len(unevaluable),
            of=len(evaluated),
            reasons=dict(by_reason),
            symbols=sorted(unevaluable)[:20],
        )

    # Portfolio rule: at most ``daily_entry_cap`` NEW entries per session, filled
    # chronologically — earlier bins already consumed part of today's budget.
    taken = entries_taken_today(ledger_bucket, now.astimezone(_ist()).date())
    budget = max(0, cfg.daily_entry_cap - taken)
    chosen = rank_top(signals, min(cfg.universe_size, budget))
    if budget < cfg.universe_size:
        _log.info(
            "meanrev_daily_entry_budget",
            bucket_id=bucket_id,
            taken_today=taken,
            cap=cfg.daily_entry_cap,
            remaining=budget,
        )
    chosen_symbols = [c.symbol for c in chosen]
    weight = Decimal("1") / Decimal(str(len(chosen))) if chosen else Decimal("0")

    with session_scope() as session:
        # Scoped to the BIN, not the day. This cut runs 7× a session, and a
        # day-scoped delete meant each pass erased the last: only 15:16's rows
        # ever survived, and the 09:16 pass — the only one that ever reads the
        # previous session's 15:15→15:30 stub — left nothing behind at all. A
        # re-run of the SAME bin (bot restart mid-bin) still replaces cleanly,
        # which is the idempotence this delete exists for.
        session.execute(
            delete(ScannerSnapshot).where(
                ScannerSnapshot.date == scan_date,
                ScannerSnapshot.strategy_id == bucket_id,
                ScannerSnapshot.bar_key == key,
            )
        )
        session.execute(
            delete(DailyUniverse).where(
                DailyUniverse.date == scan_date,
                DailyUniverse.strategy_id == bucket_id,
                DailyUniverse.bar_key == key,
            )
        )
        # One row per EVALUATED symbol, not just the ones that crossed —
        # mirroring the gap-reversal branch below. On a zero-signal bin the old
        # loop wrote nothing at all, so "why wasn't SUZLON picked?" was
        # unanswerable from Postgres, which is the other half of why the bar
        # bug hid for a week. ``metrics`` carries whatever the cut computed
        # before it stopped, so a rejected name still reports its dislocation.
        for symbol, outcome in evaluated:
            sig = outcome.signal
            session.add(
                ScannerSnapshot(
                    date=scan_date,
                    strategy_id=bucket_id,
                    symbol=symbol,
                    bar_key=key,
                    metrics=outcome.metrics,
                    filter_results=(
                        {"reason": outcome.reason} if outcome.reason else {}
                    ),
                    # deeper dislocation ranks higher
                    rank_score=-sig.dist_pct if sig is not None else None,
                    passed=sig is not None,
                )
            )
        for rank, sig in enumerate(chosen, start=1):
            session.add(
                DailyUniverse(
                    date=scan_date,
                    strategy_id=bucket_id,
                    symbol=sig.symbol,
                    bar_key=key,
                    rank=rank,
                    target_weight=weight,
                    notes=f"bar={sig.bar_key} dist={sig.dist_pct}% ema20={sig.ema20}",
                )
            )
        session.add(
            AuditLog(
                strategy_id=bucket_id,
                event_type=AuditEventType.SCANNER_RUN,
                message=(
                    f"meanrev 1h scan [{key}]: {len(chosen)}/{len(signals)} crossed "
                    f"of {len(evaluated)} evaluated (day budget {budget}); "
                    f"outcomes {dict(sorted(by_reason.items()))}"
                ),
                payload={
                    "bucket_id": bucket_id,
                    "date": str(scan_date),
                    "bar_key": key,
                    "universe": chosen_symbols,
                    # configured → attempted → evaluated, so the scan_coverage
                    # invariant can tell the three failure modes apart:
                    # the universe collapsed (attempted 0 of a non-empty
                    # configured list), every fetch failed (evaluated 0 of a
                    # non-empty attempted list), or the data was there but
                    # unusable (unevaluable ≈ evaluated).
                    "configured": len(cfg.symbols),
                    "attempted": len(symbols),
                    "evaluated": len(evaluated),
                    "passed": len(signals),
                    "outcomes": dict(by_reason),
                    "unevaluable": len(unevaluable),
                    "entries_taken_today": taken,
                },
            )
        )

    _log.info(
        "meanrev_scan_complete",
        bucket_id=bucket_id,
        bar_key=key,
        universe=chosen_symbols,
        evaluated=len(evaluated),
        passed=len(signals),
        outcomes=dict(by_reason),
    )
    result = ScanResult(
        bucket_id=bucket_id,
        date=scan_date,
        universe=chosen_symbols,
        evaluated_count=len(evaluated),
    )
    _MEANREV_SCAN_CACHE[bucket_id] = (key, result)
    # A bin nobody could read is not a settled bin. Record the attempt so the
    # next ticks re-fetch it (bounded by _BLIND_RETRY_MAX/_GAP) rather than
    # serving the blind verdict back from cache until the bin rolls.
    if not evaluated or len(unevaluable) >= len(evaluated):
        seen_key, attempts, _ = _MEANREV_BLIND_ATTEMPTS.get(
            bucket_id, ("", 0, now)
        )
        _MEANREV_BLIND_ATTEMPTS[bucket_id] = (
            key,
            attempts + 1 if seen_key == key else 1,
            now,
        )
    else:
        _MEANREV_BLIND_ATTEMPTS.pop(bucket_id, None)
    return result


def _ist():  # noqa: ANN202 — tiny local helper, typed at the call site
    from src.shared.scanner.meanrev import IST

    return IST


def _intraday_history(data: MarketData, symbol: str, days: int) -> list:
    """15m bars over ``days`` calendar days, or the adapter's default window.

    ``DhanData.get_ohlcv_history`` widens the intraday request beyond the
    5-day default the tick paths use — the 1h EMA20 needs a warm series.
    Adapters without it (tests, crypto) fall back to ``get_ohlcv``.
    """
    if hasattr(data, "get_ohlcv_history"):
        return data.get_ohlcv_history(symbol, "15m", days=days)
    return data.get_ohlcv(symbol, "15m")


def run_gap_reversal_scan(
    *,
    bucket_id: str,
    data: MarketData,
    config: ScannerConfig,
    scan_date: date_type,
) -> ScanResult:
    """Morning gap-down cut for the intraday-indian bucket (Decision 029).

    Runs the screen ONCE per session and caches it. The inputs are the 09:15
    open, the 09:25 close, and the prior session's closes — all fixed the
    moment 09:30 passes — so re-screening on every 60s tick would burn ~200
    Dhan calls a minute to recompute a constant. Instead the first pass of the
    day persists ``ScannerSnapshot`` rows (one per evaluated symbol, the
    "screen ran today" marker) plus the ``DailyUniverse`` cut, and later ticks
    read the universe straight back.

    The reversal candle that actually triggers an entry is NOT evaluated here —
    that is a live per-tick decision in ``gap_down_reversal.select_entries``.
    """
    from src.shared.scanner.gap_reversal import (
        GapCandidate,
        GapReversalConfig,
        GapScreenOutcome,
        rank_top,
        screen_with_reason,
    )

    gcfg = GapReversalConfig.from_scanner_config(config)

    # Already screened today? Replay the persisted cut instead of re-fetching.
    #
    # "Screened" means the screen actually SAW something. A pass where every
    # symbol failed on a ``data_`` reason answered no question — it only proves
    # the data was not there yet — and treating it as the day's screen is what
    # let one bad morning blank the bucket until midnight. Same distinction the
    # sizer and the scanner cut already draw: could-not-evaluate is not
    # did-not-qualify.
    #
    # Bounded, though. A retry costs a full re-fetch (~230 symbols x 2 calls,
    # measured at 113s), the tick is 60s, and Dhan caps at 5 req/s on a token
    # shared with swing-indian — so an unbounded retry would serialise into an
    # hour of continuous scanning and manufacture the 429 storm of 2026-07-22,
    # which is the very blindness this is meant to prevent. After
    # _MAX_SCREEN_ATTEMPTS the empty cut stands for the day and says so.
    with session_scope() as session:
        rows = session.execute(
            select(ScannerSnapshot.filter_results).where(
                ScannerSnapshot.date == scan_date,
                ScannerSnapshot.strategy_id == bucket_id,
            )
        ).scalars().all()
        already_ran = len(rows)
        reasons = [str((fr or {}).get("reason", "")) for fr in rows]
        attempts = session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.strategy_id == bucket_id,
                AuditLog.event_type == AuditEventType.SCANNER_RUN,
                AuditLog.ts >= _utc_start_of(scan_date),
            )
        ).scalar_one()
        data_limited = bool(reasons) and all(r.startswith("data_") for r in reasons)
        if should_rescreen(reasons, attempts):
            _log.info(
                "gap_scan_retry_data_limited",
                bucket_id=bucket_id,
                date=str(scan_date),
                attempt=attempts + 1,
                of=_MAX_SCREEN_ATTEMPTS,
                unevaluable=already_ran,
            )
            already_ran = 0  # fall through and re-screen
        elif data_limited:
            _log.warning(
                "gap_scan_gave_up_data_limited",
                bucket_id=bucket_id,
                date=str(scan_date),
                attempts=attempts,
                hint="no symbol was evaluable all morning — the cut stands empty",
            )
        if already_ran:
            cached = (
                session.execute(
                    select(DailyUniverse)
                    .where(
                        DailyUniverse.date == scan_date,
                        DailyUniverse.strategy_id == bucket_id,
                    )
                    .order_by(DailyUniverse.rank)
                )
                .scalars()
                .all()
            )
            return ScanResult(
                bucket_id=bucket_id,
                date=scan_date,
                universe=[r.symbol for r in cached],
                evaluated_count=already_ran,
            )

    # First pass of the session — run the real screen.
    # Circuit-lock screen (Decision 029). Applies only where configured — the
    # NIFTY-100 set leaves it at 0 because every constituent is an F&O
    # underlying anyway. Skipped names are logged, never silently dropped.
    symbols = list(gcfg.symbols)
    if gcfg.min_circuit_band_pct > 0 and hasattr(data, "circuit_safe"):
        safe = [s for s in symbols if data.circuit_safe(s, gcfg.min_circuit_band_pct)]
        if len(safe) != len(symbols):
            _log.info(
                "gap_scan_circuit_filtered",
                bucket_id=bucket_id,
                excluded=sorted(set(symbols) - set(safe)),
                kept=len(safe),
                of=len(symbols),
            )
        symbols = safe

    candidates: list[GapCandidate] = []
    evaluated: list[tuple[str, GapScreenOutcome]] = []
    for symbol in symbols:
        try:
            intraday = data.get_ohlcv(symbol, "5m")
            daily = data.get_ohlcv(symbol, "1d", limit=gcfg.atr_period + 30)
        except Exception:
            # A single unfetchable name must not sink the whole morning cut.
            _log.warning("gap_scan_fetch_failed", symbol=symbol, exc_info=True)
            continue
        outcome = screen_with_reason(symbol, intraday, daily, scan_date, gcfg)
        evaluated.append((symbol, outcome))
        if outcome.candidate is not None:
            candidates.append(outcome.candidate)

    # Decision 033: a symbol we could not evaluate is a DATA problem and must
    # never be mistaken for "did not qualify". Surface the count up front —
    # 99/99 unevaluable and 99/99 flat both produce zero candidates.
    unevaluable = [sym for sym, o in evaluated if not o.data_ok]
    if unevaluable:
        _log.warning(
            "gap_scan_symbols_unevaluable",
            bucket_id=bucket_id,
            count=len(unevaluable),
            of=len(evaluated),
            symbols=sorted(unevaluable)[:20],
        )

    chosen = rank_top(candidates, gcfg.universe_size)
    chosen_symbols = [c.symbol for c in chosen]
    weight = Decimal("1") / Decimal(str(len(chosen))) if chosen else Decimal("0")

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
        # Screened ONCE per session, so the bar it is about IS the day and the
        # day-scoped deletes above are already exact.
        day_bar = scan_date.isoformat()
        for symbol, outcome in evaluated:
            cand = outcome.candidate
            session.add(
                ScannerSnapshot(
                    date=scan_date,
                    strategy_id=bucket_id,
                    symbol=symbol,
                    bar_key=day_bar,
                    # Whatever the screen computed before it stopped — so a
                    # rejected symbol still reports the gap it actually had.
                    metrics=outcome.metrics,
                    filter_results={"reason": outcome.reason} if outcome.reason else {},
                    rank_score=abs(cand.gap_pct) if cand is not None else None,
                    passed=cand is not None,
                )
            )
        for rank, c in enumerate(chosen, start=1):
            session.add(
                DailyUniverse(
                    date=scan_date,
                    strategy_id=bucket_id,
                    symbol=c.symbol,
                    bar_key=day_bar,
                    rank=rank,
                    target_weight=weight,
                    notes=f"gap={c.gap_pct:.2f}% body/atr={c.body_atr_ratio:.2f}",
                )
            )
        session.add(
            AuditLog(
                strategy_id=bucket_id,
                event_type=AuditEventType.SCANNER_RUN,
                message=(
                    f"gap-reversal scan: {len(chosen)}/{len(evaluated)} gapped down, "
                    f"top-{gcfg.universe_size}"
                ),
                payload={
                    "bucket_id": bucket_id,
                    "date": str(scan_date),
                    "universe": chosen_symbols,
                    # See the meanrev payload — same three-stage funnel, read
                    # by the scan_coverage invariant.
                    "configured": len(gcfg.symbols),
                    "attempted": len(symbols),
                    "evaluated": len(evaluated),
                    "passed": len(candidates),
                    "unevaluable": len(unevaluable),
                },
            )
        )

    _log.info(
        "gap_reversal_scan_complete",
        bucket_id=bucket_id,
        date=str(scan_date),
        universe=chosen_symbols,
        evaluated=len(evaluated),
        passed=len(candidates),
    )
    return ScanResult(
        bucket_id=bucket_id,
        date=scan_date,
        universe=chosen_symbols,
        evaluated_count=len(evaluated),
    )
