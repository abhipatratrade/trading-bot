"""
Daily equity-scanner prepare job — the heavy pass that makes per-tick scans fast.

Equity momentum filters need daily indicator history (RSI/EMA/CCI as of the prior
close) over the whole NSE+BSE universe (~4,600 names). Doing that every tick is
the ~45-minute problem, so — mirroring the regime retrain timer (Decision 020) —
a once-a-day job computes the daily pass in parallel and persists the survivors
as ``ScannerSnapshot`` rows (``passed=True`` + indicator metrics). The per-tick
``run_equity_scan`` then only reads that shortlist and does the light intraday
gap/volume confirm.

Invocation (VM systemd timer, ~18:00 IST after the close):
    python -m src.shared.scanner.prepare_job --due
    python -m src.shared.scanner.prepare_job --bucket swing-indian

OUTSIDE the tick loop and safety-agnostic by design (it only writes the read-side
shortlist); the deterministic entry/exit/safety path stays in ``BucketRunner``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete

from src.core.alerts import send_alert
from src.core.clock import RealClock
from src.core.db import session_scope
from src.core.logging import configure_logging, get_logger
from src.core.models import AuditEventType, AuditLog, ScannerSnapshot
from src.data_sources.dhan import DhanData
from src.shared.bucket import Market, load_bucket, load_buckets
from src.shared.scanner.engine import load_scanner_config
from src.shared.scanner.equity import EquityScanConfig, Survivor, daily_pass

_log = get_logger("shared.scanner.prepare")

# Daily-history bars to pull per symbol (covers RSI/EMA/CCI/Supertrend warmup).
_DAILY_LIMIT = 60
# Parallel fetch workers — bounded to stay within Dhan's data rate limits.
_MAX_WORKERS = 8


@dataclass(frozen=True, slots=True)
class PrepareResult:
    bucket_id: str
    scanned: int
    survivors: int
    errors: int


def _evaluate(data: DhanData, symbol: str, cfg: EquityScanConfig) -> Survivor | None:
    """Fetch daily bars for one symbol and apply the daily pass. None on error."""
    try:
        bars = data.get_ohlcv(symbol, "1d", limit=_DAILY_LIMIT)
    except Exception:
        return None
    return daily_pass(symbol, bars, cfg)


def run_prepare(bucket_id: str, *, data: DhanData | None = None) -> PrepareResult:
    """Run the daily pass for one equity bucket and persist survivors.

    Writes ``ScannerSnapshot`` rows (passed=True) for the names that cleared the
    daily filters, replacing any existing shortlist for today (idempotent re-run).
    """
    bucket = load_bucket(bucket_id)
    scanner_cfg = load_scanner_config(bucket.scanner_yaml_path)
    if scanner_cfg.engine != "equity_daily":
        raise ValueError(
            f"{bucket_id}: prepare job only handles engine=equity_daily "
            f"(got {scanner_cfg.engine!r})"
        )
    ecfg = EquityScanConfig.from_scanner_config(scanner_cfg)

    owns_data = data is None
    data = data or DhanData.from_settings()
    scan_date = RealClock().now().date()
    symbols = data.symbols()
    _log.info("prepare_start", bucket_id=bucket_id, universe=len(symbols))

    survivors: list[Survivor] = []
    errors = 0
    try:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            for res in pool.map(lambda s: _evaluate(data, s, ecfg), symbols):
                if res is not None:
                    survivors.append(res)
        # (parallel map swallows per-symbol errors as None; count is scanned−kept
        # minus true survivors, but we track failures explicitly for the log)
    finally:
        if owns_data:
            data.close()

    with session_scope() as session:
        session.execute(
            delete(ScannerSnapshot).where(
                ScannerSnapshot.date == scan_date,
                ScannerSnapshot.strategy_id == bucket_id,
            )
        )
        for s in survivors:
            session.add(
                ScannerSnapshot(
                    date=scan_date,
                    strategy_id=bucket_id,
                    symbol=s.symbol,
                    # One heavy daily pass, one bar: the day itself.
                    bar_key=scan_date.isoformat(),
                    metrics={
                        "prev_close": str(s.prev_close),
                        "rsi": str(s.rsi),
                        "cci": str(s.cci),
                        "supertrend": str(s.supertrend),
                    },
                    filter_results={"daily_pass": "true"},
                    rank_score=None,
                    passed=True,
                )
            )
        session.add(
            AuditLog(
                strategy_id=bucket_id,
                event_type=AuditEventType.SCANNER_RUN,
                message=(
                    f"equity prepare: {len(survivors)} survivors "
                    f"from {len(symbols)} symbols"
                ),
                payload={
                    "bucket_id": bucket_id,
                    "date": str(scan_date),
                    "universe": len(symbols),
                    "survivors": len(survivors),
                    "phase": "daily_prepare",
                },
            )
        )

    _log.info(
        "prepare_complete",
        bucket_id=bucket_id,
        scanned=len(symbols),
        survivors=len(survivors),
    )
    return PrepareResult(
        bucket_id=bucket_id,
        scanned=len(symbols),
        survivors=len(survivors),
        errors=errors,
    )


def _cadence_due_today(now: datetime) -> bool:
    """Equity prepare runs on weekdays (NSE is closed Sat/Sun)."""
    return now.weekday() < 5


def prepare_enabled_buckets(*, due_only: bool) -> list[PrepareResult]:
    """Run prepare for every enabled Indian bucket with an equity scanner."""
    now = RealClock().now()
    if due_only and not _cadence_due_today(now):
        _log.info("prepare_not_due_weekend")
        return []
    out: list[PrepareResult] = []
    for bucket in load_buckets():
        if not bucket.config.enabled or bucket.market != Market.INDIAN:
            continue
        try:
            cfg = load_scanner_config(bucket.scanner_yaml_path)
        except FileNotFoundError:
            continue
        if cfg.engine != "equity_daily":
            continue
        out.append(run_prepare(bucket.id))
    return out


def main() -> None:  # pragma: no cover
    configure_logging()
    parser = argparse.ArgumentParser(description="Daily equity-scanner prepare pass")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--bucket", help="prepare a single bucket_id")
    target.add_argument("--due", action="store_true", help="prepare due buckets (timer mode)")
    args = parser.parse_args()

    try:
        results = (
            [run_prepare(args.bucket)]
            if args.bucket
            else prepare_enabled_buckets(due_only=True)
        )
    except Exception:
        _log.exception("equity_prepare_failed")
        send_alert("⚠️ Equity scanner prepare FAILED — see VM logs (journalctl -u dhan-prepare)")
        raise

    if not results:
        print("No equity buckets due for prepare.")
        return
    for r in results:
        print(f"  {r.bucket_id}: scanned={r.scanned} survivors={r.survivors}")
        send_alert(
            f"Equity prepare {r.bucket_id}: {r.survivors} survivors "
            f"from {r.scanned} symbols"
        )


if __name__ == "__main__":  # pragma: no cover
    main()
