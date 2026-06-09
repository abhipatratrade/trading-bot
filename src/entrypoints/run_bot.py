"""
Bot worker entrypoint — boots BucketRunner per (type × market) bucket.

Lifecycle:
  1. Load settings + buckets.yaml.
  2. Init broker + data source clients (per bucket's broker).
  3. Run reconciler once at startup.
  4. Refresh symbol mappings.
  5. Enter main loop: tick every enabled bucket, sleep, repeat.
  6. On shutdown: close connections gracefully.

Usage:
    python -m src.entrypoints.run_bot
"""

from __future__ import annotations

import signal
import time

from src.brokers.base import Broker
from src.brokers.delta_india.client import DeltaIndiaClient
from src.core.alerts import send_alert
from src.core.clock import RealClock
from src.core.config import get_settings
from src.core.db import session_scope
from src.core.logging import configure_logging, get_logger
from src.core.models import AuditEventType, AuditLog, BrokerName
from src.data_sources.binance import BinanceData
from src.data_sources.delta_india import DeltaIndiaData
from src.data_sources.symbol_loader import (
    DEFAULT_CSV,
    fetch_mappings,
    load_csv,
    load_to_db,
)
from src.order_manager.manager import OrderManager
from src.order_manager.reconciler import Reconciler
from src.shared.bucket import Market, load_buckets
from src.shared.bucket_runner import BucketRunner

TICK_INTERVAL_SECONDS = 60

_log = get_logger("entrypoints.run_bot")
_shutdown = False
_tick_count = 0


def _handle_signal(signum: int, _frame: object) -> None:
    global _shutdown
    _log.info("shutdown_signal_received", signal=signum)
    _shutdown = True


def main() -> None:
    global _shutdown
    configure_logging()
    settings = get_settings()
    clock = RealClock()

    _log.info("bot_starting", trading_mode=settings.trading_mode.value)

    # Log outbound IP for Delta whitelist.
    try:
        import httpx as _httpx

        _ip = _httpx.get("https://api.ipify.org", timeout=5).text.strip()
        _log.info("OUTBOUND_IP", ip=_ip)
    except Exception:
        _log.warning("outbound_ip_check_failed")

    # ── Clients ─────────────────────────────────────────────────────────
    delta_client = DeltaIndiaClient(settings)
    delta_data = DeltaIndiaData(settings)

    brokers: dict[BrokerName, Broker] = {BrokerName.DELTA_INDIA: delta_client}
    order_managers: dict[BrokerName, OrderManager] = {
        BrokerName.DELTA_INDIA: OrderManager(
            delta_client, BrokerName.DELTA_INDIA, clock
        ),
    }
    # Dhan adapter lands in Phase 3 — Indian buckets stay disabled until then.

    reconciler = Reconciler(delta_client, BrokerName.DELTA_INDIA)

    # ── Symbol mappings ─────────────────────────────────────────────────
    _log.info("refreshing_symbol_mappings")
    try:
        binance_data = BinanceData(settings)
        mappings = fetch_mappings(binance_data, delta_data)
        count = load_to_db(mappings)
        _log.info("symbol_mappings_refreshed", count=count)
        binance_data.close()
    except Exception:
        _log.warning("symbol_mapping_api_failed_using_csv", exc_info=True)
        if DEFAULT_CSV.exists():
            try:
                rows = load_csv(DEFAULT_CSV)
                count = load_to_db(rows)
                _log.info(
                    "symbol_mappings_loaded_from_csv",
                    count=count,
                    path=str(DEFAULT_CSV),
                )
            except Exception:
                _log.error("symbol_mapping_csv_load_failed", exc_info=True)
        else:
            _log.error("symbol_mapping_csv_not_found", path=str(DEFAULT_CSV))

    # ── Buckets ─────────────────────────────────────────────────────────
    all_buckets = load_buckets()
    runners: list[BucketRunner] = []
    for bucket in all_buckets:
        if not bucket.config.enabled:
            _log.info("bucket_skipped_disabled", bucket_id=bucket.id)
            continue
        # For now, only crypto buckets have a data source (Delta India).
        # Indian buckets will need a Dhan data source in Phase 3.
        if bucket.market != Market.CRYPTO:
            _log.warning(
                "bucket_skipped_no_data_source",
                bucket_id=bucket.id,
                market=bucket.market.value,
            )
            continue
        try:
            runner = BucketRunner(
                bucket=bucket,
                brokers=brokers,
                data=delta_data,
                order_managers=order_managers,
                clock=clock,
            )
        except Exception:
            _log.error(
                "bucket_init_failed", bucket_id=bucket.id, exc_info=True
            )
            send_alert(f"[bot] bucket {bucket.id} failed to init — see logs")
            continue
        runners.append(runner)
        _log.info(
            "bucket_initialised",
            bucket_id=bucket.id,
            strategies=list(runner.strategies.keys()),
        )

    if not runners:
        _log.error("no_enabled_buckets")
        send_alert("[bot] No enabled buckets — nothing to do")
        return

    # ── Startup reconcile ───────────────────────────────────────────────
    _log.info("startup_reconcile")
    try:
        report = reconciler.run()
        _log.info(
            "startup_reconcile_done",
            positions_updated=report.positions_updated,
            positions_closed=report.positions_closed,
            orphan_positions=report.orphan_positions,
            orders_updated=report.orders_updated,
        )
    except Exception:
        _log.error("startup_reconcile_failed", exc_info=True)

    # ── Audit + alert startup ───────────────────────────────────────────
    bucket_ids = [r.bucket.id for r in runners]
    with session_scope() as session:
        session.add(
            AuditLog(
                event_type=AuditEventType.BOT_STARTUP,
                message=f"Bot started (mode={settings.trading_mode.value})",
                payload={
                    "trading_mode": settings.trading_mode.value,
                    "buckets": bucket_ids,
                },
            )
        )
    send_alert(
        f"Bot started (mode={settings.trading_mode.value})\n"
        f"Buckets: {bucket_ids}"
    )

    # ── Signal handlers ─────────────────────────────────────────────────
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ── Main loop ───────────────────────────────────────────────────────
    _log.info(
        "main_loop_starting",
        tick_interval=TICK_INTERVAL_SECONDS,
        buckets=bucket_ids,
    )
    while not _shutdown:
        for runner in runners:
            try:
                runner.run_once()
            except KeyboardInterrupt:
                _shutdown = True
                break
            except Exception:
                _log.error(
                    "bucket_tick_error",
                    bucket_id=runner.bucket.id,
                    exc_info=True,
                )
                send_alert(f"[bot] tick error in {runner.bucket.id}")
        if _shutdown:
            break

        global _tick_count
        _tick_count += 1
        if _tick_count % 5 == 0:
            try:
                reconciler.run()
            except Exception:
                _log.error("periodic_reconcile_failed", exc_info=True)

        for _ in range(TICK_INTERVAL_SECONDS):
            if _shutdown:
                break
            time.sleep(1)

    # ── Shutdown ────────────────────────────────────────────────────────
    _log.info("bot_shutting_down")
    with session_scope() as session:
        session.add(
            AuditLog(
                event_type=AuditEventType.BOT_SHUTDOWN,
                message="Bot shutting down (signal received)",
            )
        )
    send_alert("Bot shutting down")
    delta_client.close()
    delta_data.close()
    _log.info("bot_stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
