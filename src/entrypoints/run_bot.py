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
from decimal import Decimal

from src.brokers.base import Broker
from src.brokers.delta_india.client import DeltaIndiaClient
from src.brokers.dhan.client import DhanClient
from src.core.alerts import note_alert_recovery, send_alert, send_alert_dedup
from src.core.clock import RealClock
from src.core.config import get_settings
from src.core.db import session_scope
from src.core.heartbeat import SERVICE_BOT_WORKER, beat
from src.core.logging import configure_logging, get_logger
from src.core.models import AuditEventType, AuditLog, BrokerName
from src.data_sources.base import MarketData
from src.data_sources.binance import BinanceData
from src.data_sources.delta_india import DeltaIndiaData
from src.data_sources.dhan import BOT_REQUEST_DELAY_SECONDS, DhanData
from src.data_sources.symbol_loader import (
    DEFAULT_CSV,
    fetch_mappings,
    load_csv,
    load_to_db,
)
from src.order_manager.manager import OrderManager
from src.order_manager.reconciler import Reconciler
from src.safety.enforcement import enforce_breakers
from src.safety.stop_protection import ensure_stop_protection
from src.shared.allocator.sizer import load_allocator_config
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
    bucket_fx: dict[str, Decimal] = {}
    stop_pcts: dict[str, Decimal] = {}
    for bucket in all_buckets:
        if not bucket.config.enabled or bucket.market != Market.CRYPTO:
            continue
        if bucket.config.broker != BrokerName.DELTA_INDIA:
            continue
        accounts.setdefault(bucket.config.account_ref, []).append(bucket.id)
        # Broker-side protective stop distance (Decision 022).
        if bucket.config.stop_loss_pct is not None:
            stop_pcts[bucket.id] = bucket.config.stop_loss_pct
        # FX for the reconciler's wallet→bucket_state sync (Decision 021).
        try:
            bucket_fx[bucket.id] = load_allocator_config(
                bucket.allocator_yaml_path
            ).fx_inr_per_usd
        except Exception:
            _log.warning(
                "allocator_fx_load_failed_using_1",
                bucket_id=bucket.id,
                exc_info=True,
            )

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
            client,
            BrokerName.DELTA_INDIA,
            clock,
            bucket_ids=ref_bucket_ids,
            bucket_fx={
                b: fx for b, fx in bucket_fx.items() if b in ref_bucket_ids
            },
        )
        _log.info("delta_account_ready", account_ref=ref, buckets=ref_bucket_ids)

    # Per-account market-data source: crypto accounts use the shared Delta feed;
    # Dhan accounts use the Dhan feed (built below).
    data_by_ref: dict[str, MarketData] = dict.fromkeys(accounts, delta_data)

    # ── Dhan account (Indian equity buckets, Phase 3/4) ─────────────────
    # FAIL-SOFT: if Dhan creds/data are unavailable, the Indian bucket is
    # skipped and the crypto bot keeps running. Enabling swing-indian in
    # buckets.yaml must never be able to take down the live crypto path.
    dhan_data: DhanData | None = None
    dhan_accounts: dict[str, list[str]] = {}
    for bucket in all_buckets:
        if (
            bucket.config.enabled
            and bucket.market == Market.INDIAN
            and bucket.config.broker == BrokerName.DHAN
        ):
            dhan_accounts.setdefault(bucket.config.account_ref, []).append(bucket.id)
            bucket_fx[bucket.id] = Decimal("1")  # Dhan wallet is INR-native
            if bucket.config.stop_loss_pct is not None:
                stop_pcts[bucket.id] = bucket.config.stop_loss_pct
    if dhan_accounts:
        try:
            # Pace charts calls under Dhan's 5 req/s Data-API cap: the
            # intraday-indian morning scan fetches ~2 calls/symbol across the
            # NIFTY-100 (Decision 029). Single-fetch paths (swing exits) only
            # eat the small per-call delay.
            dhan_data = DhanData.from_settings(
                settings, request_delay_seconds=BOT_REQUEST_DELAY_SECONDS
            )
            for ref, ref_bucket_ids in dhan_accounts.items():
                client = DhanClient.from_settings(
                    dhan_data.resolve,
                    settings,
                    data_token_manager=dhan_data.token_manager,
                )
                # Fail-fast reachability probe (one authed GET). Dhan's
                # SANDBOX edge blocks datacenter IPs with a bodyless 403
                # (confirmed from the GCP VM 2026-07-12: sandbox 403, live
                # 401) — surface that here as a clean init failure (bucket
                # skipped + one alert) instead of breaker/stop-sweep ERROR
                # spam every tick against an unreachable host.
                client.get_balances()
                brokers[ref] = client
                order_managers[ref] = OrderManager(client, BrokerName.DHAN, clock)
                reconcilers[ref] = Reconciler(
                    client,
                    BrokerName.DHAN,
                    clock,
                    bucket_ids=ref_bucket_ids,
                    bucket_fx={b: Decimal("1") for b in ref_bucket_ids},
                    # The Dhan account is SHARED with the user's manual trading
                    # (Decision 027) — only manage positions the bot opened.
                    shared_account=True,
                )
                accounts[ref] = ref_bucket_ids  # breakers/stops/reconcile loops
                data_by_ref[ref] = dhan_data
                _log.info(
                    "dhan_account_ready", account_ref=ref, buckets=ref_bucket_ids
                )
        except Exception:
            _log.error("dhan_account_init_failed", exc_info=True)
            send_alert(
                "[bot] Dhan account init FAILED — swing-indian will NOT run "
                "(crypto buckets unaffected). Causes: bad Dhan creds on the "
                "VM, or the Dhan sandbox edge-blocking this host's IP "
                "(datacenter IPs get 403; awaiting Dhan support)."
            )
            dhan_data = None
            # Roll back partial Dhan wiring so the runner loop skips Indian buckets.
            for ref in list(dhan_accounts):
                brokers.pop(ref, None)
                order_managers.pop(ref, None)
                reconcilers.pop(ref, None)
                accounts.pop(ref, None)
                data_by_ref.pop(ref, None)

    runners: list[BucketRunner] = []
    for bucket in all_buckets:
        if not bucket.config.enabled:
            _log.info("bucket_skipped_disabled", bucket_id=bucket.id)
            continue
        # A bucket runs only if its account has a data source wired above
        # (crypto → Delta; Indian → Dhan). Missing ⇒ skip (e.g. Dhan creds
        # absent), never crash the loop.
        bucket_data = data_by_ref.get(bucket.config.account_ref)
        if bucket_data is None:
            _log.warning(
                "bucket_skipped_no_data_source",
                bucket_id=bucket.id,
                market=bucket.market.value,
                account_ref=bucket.config.account_ref,
            )
            continue
        try:
            runner = BucketRunner(
                bucket=bucket,
                brokers=brokers,
                data=bucket_data,
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

    # ── Protective stop-loss sweep (Decision 022) ───────────────────────
    # Idempotent: place missing stops, resize on adds, cancel orphans.
    # Runs at startup and once per tick (after the runners, so fresh
    # entries are protected within seconds).
    def _sweep_stops() -> None:
        for ref, ref_bucket_ids in accounts.items():
            pcts = {b: p for b, p in stop_pcts.items() if b in ref_bucket_ids}
            if not pcts:
                continue
            try:
                ensure_stop_protection(
                    account_ref=ref,
                    bucket_ids=ref_bucket_ids,
                    broker=brokers[ref],
                    order_manager=order_managers[ref],
                    stop_pct_by_bucket=pcts,
                    clock=clock,
                    shared_account=ref in dhan_accounts,
                )
            except Exception:
                _log.error(
                    "stop_protection_sweep_failed", account_ref=ref, exc_info=True
                )
                send_alert_dedup(
                    f"stop_sweep_error:{ref}",
                    f"[bot] stop-protection sweep ERROR on account {ref} — see logs",
                )

    _sweep_stops()

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
    dd_pct = Decimal(str(settings.daily_drawdown_pct))
    liq_pct = Decimal(str(settings.liquidation_distance_min_pct))
    funding_max = Decimal(str(settings.funding_rate_max))

    # Per-bucket cadence: full pipeline passes are paced to the bucket TF
    # (a 1d bucket re-scans every 15 min, not every 60s). Safety below
    # (breakers, stop sweep, heartbeat) stays on the 60s loop.
    next_due: dict[str, float] = {r.bucket.id: 0.0 for r in runners}
    for r in runners:
        _log.info(
            "bucket_cadence",
            bucket_id=r.bucket.id,
            tick_interval_seconds=r.tick_interval_seconds,
        )

    while not _shutdown:
        # ── Safety first: breakers per sub-account (Decision 021) ───────
        # A trip engages the per-bucket kill switch and flattens the
        # account; runners below then skip those buckets.
        for ref, ref_bucket_ids in accounts.items():
            try:
                enforce_breakers(
                    account_ref=ref,
                    bucket_ids=ref_bucket_ids,
                    broker=brokers[ref],
                    order_manager=order_managers[ref],
                    data=data_by_ref.get(ref, delta_data),
                    max_drawdown_pct=dd_pct,
                    min_liq_distance_pct=liq_pct,
                    max_funding_rate=funding_max,
                    clock=clock,
                    shared_account=ref in dhan_accounts,
                )
            except Exception:
                _log.error(
                    "breaker_enforcement_error", account_ref=ref, exc_info=True
                )
                send_alert_dedup(
                    f"breaker_error:{ref}",
                    f"[bot] breaker enforcement ERROR on account {ref} — see logs",
                )

        for runner in runners:
            if time.monotonic() < next_due[runner.bucket.id]:
                continue
            # Schedule the next pass up front so a crashing bucket backs
            # off to its cadence instead of hot-looping every 60s.
            next_due[runner.bucket.id] = (
                time.monotonic() + runner.tick_interval_seconds
            )
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

        # Protect every position opened/changed this tick (Decision 022).
        _sweep_stops()

        # Dead-man's switch: certify this tick completed. The Railway
        # scheduler pages if this row goes stale (heartbeat_stale_seconds).
        beat(SERVICE_BOT_WORKER, clock)

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
    if dhan_data is not None:
        dhan_data.close()
    _log.info("bot_stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
