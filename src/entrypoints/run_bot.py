"""
Bot worker entrypoint — boots strategies, runs the main loop.

Lifecycle:
  1. Load config, validate policy.yaml files.
  2. Init broker + data source clients.
  3. Run reconciler once at startup.
  4. Enter main loop: tick each strategy, sleep, repeat.
  5. On shutdown: close connections gracefully.

Usage:
    python -m src.entrypoints.run_bot
"""

from __future__ import annotations

import signal
import time

from src.brokers.delta_india.client import DeltaIndiaClient
from src.core.alerts import send_alert
from src.core.clock import RealClock
from src.core.config import get_settings
from src.core.db import session_scope
from src.core.logging import configure_logging, get_logger
from src.core.models import AuditEventType, AuditLog, BrokerName
from src.data_sources.binance import BinanceData
from src.data_sources.delta_india import DeltaIndiaData
from src.data_sources.symbol_loader import DEFAULT_CSV, fetch_mappings, load_csv, load_to_db
from src.order_manager.manager import OrderManager
from src.order_manager.reconciler import Reconciler
from src.strategies.crypto_longterm.params import load_and_audit
from src.strategies.crypto_longterm.runner import CryptoLongtermRunner

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

    _log.info(
        "bot_starting",
        trading_mode=settings.trading_mode.value,
    )

    # TEMP: log current outbound IP — remove after whitelisting
    try:
        import httpx as _httpx
        _ip = _httpx.get("https://api.ipify.org", timeout=5).text.strip()
        _log.info("RAILWAY_OUTBOUND_IP", ip=_ip)
    except Exception:
        pass

    # -- Init clients --
    broker = DeltaIndiaClient(settings)
    data_source = DeltaIndiaData(settings)
    order_manager = OrderManager(broker, BrokerName.DELTA_INDIA, clock)
    reconciler = Reconciler(broker, BrokerName.DELTA_INDIA)

    # -- Refresh symbol mappings --
    # Binance Futures API is geo-blocked in some Railway regions (HTTP 451).
    # Fall back to the committed CSV so the scanner always has data.
    _log.info("refreshing_symbol_mappings")
    try:
        binance_data = BinanceData(settings)
        mappings = fetch_mappings(binance_data, data_source)
        count = load_to_db(mappings)
        _log.info("symbol_mappings_refreshed", count=count)
        binance_data.close()
    except Exception:
        _log.warning("symbol_mapping_api_failed_using_csv", exc_info=True)
        if DEFAULT_CSV.exists():
            try:
                rows = load_csv(DEFAULT_CSV)
                count = load_to_db(rows)
                _log.info("symbol_mappings_loaded_from_csv", count=count, path=str(DEFAULT_CSV))
            except Exception:
                _log.error("symbol_mapping_csv_load_failed", exc_info=True)
        else:
            _log.error("symbol_mapping_csv_not_found", path=str(DEFAULT_CSV))

    # -- Load strategy config --
    policy = load_and_audit()

    # -- Build strategy runners --
    crypto_lt = CryptoLongtermRunner(
        policy=policy,
        broker=broker,
        data_source=data_source,
        order_manager=order_manager,
        clock=clock,
    )

    # -- Startup reconciliation --
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

    # -- Audit bot startup --
    with session_scope() as session:
        session.add(
            AuditLog(
                event_type=AuditEventType.BOT_STARTUP,
                message=f"Bot started (mode={settings.trading_mode.value})",
                payload={
                    "trading_mode": settings.trading_mode.value,
                    "strategies": [policy.strategy_id],
                },
            )
        )

    send_alert(
        f"Bot started (mode={settings.trading_mode.value})\n"
        f"Strategies: [{policy.strategy_id}]"
    )

    # -- Signal handlers --
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # -- Main loop --
    _log.info("main_loop_starting", tick_interval=TICK_INTERVAL_SECONDS)

    while not _shutdown:
        try:
            crypto_lt.tick()
        except KeyboardInterrupt:
            break
        except Exception:
            _log.error("tick_error", exc_info=True)
            send_alert(f"[bot] Tick error — check logs")

        # Periodic reconciliation (every 5 min = 5 ticks at 60s)
        global _tick_count
        _tick_count += 1
        if _tick_count % 5 == 0:
            try:
                reconciler.run()
            except Exception:
                _log.error("periodic_reconcile_failed", exc_info=True)

        # Sleep in small increments so shutdown signal is responsive
        for _ in range(TICK_INTERVAL_SECONDS):
            if _shutdown:
                break
            time.sleep(1)

    # -- Shutdown --
    _log.info("bot_shutting_down")
    with session_scope() as session:
        session.add(
            AuditLog(
                event_type=AuditEventType.BOT_SHUTDOWN,
                message="Bot shutting down (signal received)",
            )
        )

    send_alert("Bot shutting down")
    broker.close()
    data_source.close()
    _log.info("bot_stopped")


if __name__ == "__main__":
    main()
