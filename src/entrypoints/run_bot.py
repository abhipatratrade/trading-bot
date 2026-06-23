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
from src.core.alerts import note_alert_recovery, send_alert, send_alert_dedup
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

    # ── Shared market data ──────────────────────────────────────────────
    # Public Delta market data is account-agnostic — one shared instance.
    # Execution clients are built per sub-account below, after buckets load.
    delta_data = DeltaIndiaData(settings)

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
        send_alert("[bot] Symbol mapping refresh FAILED — falling back to CSV")
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
                send_alert(
                    "[bot] Symbol mapping CSV fallback ALSO FAILED — see logs"
                )
        else:
            _log.error("symbol_mapping_csv_not_found", path=str(DEFAULT_CSV))
            send_alert(
                f"[bot] Symbol mapping CSV missing at {DEFAULT_CSV} — no mappings loaded"
            )

    # ── Buckets ─────────────────────────────────────────────────────────
    all_buckets = load_buckets()

    # ── Per-account execution clients (Decision 019) ────────────────────
    # Each crypto bucket trades on its own Delta India sub-account so
    # positions, leverage, and margin are isolated. Build one client +
    # OrderManager + Reconciler per distinct account_ref among enabled
    # crypto buckets; each reconciler is scoped to its account's bucket(s).
    accounts: dict[str, list[str]] = {}
    for bucket in all_buckets:
        if not bucket.config.enabled or bucket.market != Market.CRYPTO:
            continue
        if bucket.config.broker != BrokerName.DELTA_INDIA:
            continue
        accounts.setdefault(bucket.config.account_ref, []).append(bucket.id)

    brokers: dict[str, Broker] = {}
    order_managers: dict[str, OrderManager] = {}
    reconcilers: dict[str, Reconciler] = {}
    for ref, ref_bucket_ids in accounts.items():
        try:
            creds = settings.delta_account(ref)  # fail-fast (House Rule #6)
        except ValueError:
            _log.error("delta_account_creds_missing", account_ref=ref, exc_info=True)
            send_alert(
                f"[bot] Missing Delta credentials for account_ref={ref!r} "
                f"(buckets {ref_bucket_ids}) — bot cannot start"
            )
            raise
        client = DeltaIndiaClient(
            settings,
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            base_url=creds.base_url,
        )
        brokers[ref] = client
        order_managers[ref] = OrderManager(client, BrokerName.DELTA_INDIA, clock)
        reconcilers[ref] = Reconciler(
            client, BrokerName.DELTA_INDIA, clock, bucket_ids=ref_bucket_ids
        )
        _log.info("delta_account_ready", account_ref=ref, buckets=ref_bucket_ids)

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

    # ── Startup reconcile (one pass per sub-account) ────────────────────
    _log.info("startup_reconcile")
    for ref, rec in reconcilers.items():
        try:
            report = rec.run()
            _log.info(
                "startup_reconcile_done",
                account_ref=ref,
                positions_updated=report.positions_updated,
                positions_closed=report.positions_closed,
                orphan_positions=report.orphan_positions,
                orders_updated=report.orders_updated,
            )
        except Exception:
            _log.error("startup_reconcile_failed", account_ref=ref, exc_info=True)

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
                # If this bucket had been paging tick errors, tell the
                # user it's healthy again (and re-arm the dedup channel).
                note_alert_recovery(
                    f"tick_error:{runner.bucket.id}",
                    f"[bot] {runner.bucket.id} recovered — ticks succeeding again",
                )
            except KeyboardInterrupt:
                _shutdown = True
                break
            except Exception:
                _log.error(
                    "bucket_tick_error",
                    bucket_id=runner.bucket.id,
                    exc_info=True,
                )
                send_alert_dedup(
                    f"tick_error:{runner.bucket.id}",
                    f"[bot] tick error in {runner.bucket.id}",
                )
        if _shutdown:
            break

        global _tick_count
        _tick_count += 1
        if _tick_count % 5 == 0:
            for ref, rec in reconcilers.items():
                try:
                    rec.run()
                except Exception:
                    _log.error(
                        "periodic_reconcile_failed", account_ref=ref, exc_info=True
                    )

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
    for client in brokers.values():
        try:
            client.close()
        except Exception:
            _log.warning("client_close_failed", exc_info=True)
    delta_data.close()
    _log.info("bot_stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
