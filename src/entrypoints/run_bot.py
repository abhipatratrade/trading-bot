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

from sqlalchemy import select

from src.brokers.base import Broker
from src.brokers.delta_india.client import DeltaIndiaClient
from src.brokers.dhan.client import (
    DhanClient,
    is_invalid_token_error,
    is_transient_upstream_error,
)
from src.core.alerts import (
    note_alert_recovery,
    note_sustained_failure,
    note_sustained_recovery,
    reset_alert_dedup,
    send_alert,
    send_alert_dedup,
)
from src.core.clock import RealClock
from src.core.config import get_settings
from src.core.db import session_scope
from src.core.export import archive_lag_days, audit_archived_through
from src.core.heartbeat import SERVICE_BOT_WORKER, beat
from src.core.logging import configure_logging, get_logger
from src.core.models import AuditEventType, AuditLog, BrokerName, Trade
from src.data_sources.base import MarketData
from src.data_sources.binance import BinanceData
from src.data_sources.delta_india import DeltaIndiaData
from src.data_sources.dhan import BOT_REQUEST_DELAY_SECONDS, DhanData
from src.data_sources.dhan_fno import FnoRegistry
from src.data_sources.symbol_loader import (
    DEFAULT_CSV,
    fetch_mappings,
    load_csv,
    load_to_db,
)
from src.order_manager.manager import OrderManager
from src.order_manager.reconciler import Reconciler
from src.safety.enforcement import enforce_breakers
from src.safety.session_invariants import (
    BucketWatch,
    InvariantThresholds,
    bucket_service,
    enforce_session_invariants,
    run_session_invariants,
)
from src.safety.stop_protection import ensure_stop_protection
from src.shared.allocator.sizer import load_allocator_config
from src.shared.bucket import Market, load_buckets
from src.shared.bucket_runner import BucketRunner
from src.shared.market_calendar import NseSession, nse_session

TICK_INTERVAL_SECONDS = 60

# A Dhan single-session token eviction (a second session is created for the
# account) self-heals once Dhan's "one token / 2 min" mint cooldown clears.
# Safety-path DH-906s are silenced for this grace so a routine eviction never
# pages; only a token stuck past it (bad creds/TOTP, IP block) alerts.
#
# 300s, raised from 180s on 2026-08-10 after a real eviction recovered in 186
# and paged three sweeps by SIX SECONDS. 180 was calibrated against the mint
# cooldown alone; the true worst case is longer, because the cooldown is
# server-side per CLIENT ID and the competing login starts it — so the bot's
# first attempt can be refused for reasons that predate it, and it then waits
# out someone else's two minutes plus its own tick. 130s cooldown + 60s tick +
# margin is comfortably under 300, while a token genuinely stuck (the 08-04/05
# outage ran two DAYS) still pages within five minutes.
_TOKEN_GRACE_SECONDS = 300.0

# A 5xx/timeout from the broker is upstream weather, not a bot fault, and it
# clears on the next 60s sweep — the Dhan 502 of 2026-08-09 did exactly that,
# yet paged instantly. Give it two sweeps to resolve itself before waking
# anyone. Deliberately shorter than the token grace: nothing self-heals a
# genuine outage, so the sooner it escalates the better.
_UPSTREAM_GRACE_SECONDS = 120.0

_log = get_logger("entrypoints.run_bot")
_shutdown = False
_tick_count = 0

# Dedup key for the archive dead-man's switch (see _check_archive).
_ARCHIVE_ALERT_KEY = "archive_stale"

# Ticks between archive checks. The watermark moves once a day, so anything
# faster is pure noise against the DB; hourly still catches a stall the same
# morning it matters.
_ARCHIVE_CHECK_EVERY_TICKS = 60


# Startup probe retry schedule, in seconds to wait BEFORE each attempt after
# the first. Must span Dhan's ~130s server-side mint cooldown, because a
# restart landing inside that window is exactly the case this exists for.
_PROBE_BACKOFF_SECONDS = (20.0, 60.0, 70.0)


def _probe_with_retry(client: object, account_ref: str) -> None:
    """Authed reachability probe that tolerates a self-healing token state.

    Re-raises the last error once the schedule is exhausted, so a genuinely
    bad credential or an edge-blocked IP still fails the account fast-ish and
    loudly. Only DH-906 and transient upstream errors are retried; a 403 from
    the sandbox edge is permanent and fails on the first attempt.
    """
    attempts = 1 + len(_PROBE_BACKOFF_SECONDS)
    for i in range(attempts):
        if i:
            time.sleep(_PROBE_BACKOFF_SECONDS[i - 1])
        try:
            client.get_balances()  # type: ignore[attr-defined]
            if i:
                _log.info("dhan_probe_recovered", account_ref=account_ref, attempt=i + 1)
            return
        except Exception as exc:
            healable = is_invalid_token_error(exc) or is_transient_upstream_error(exc)
            if not healable or i == attempts - 1:
                raise
            _log.warning(
                "dhan_probe_retrying",
                account_ref=account_ref,
                attempt=i + 1,
                of=attempts,
                error=str(exc)[:120],
            )


def _bot_placed_order_id(exchange_order_id: str) -> bool:
    """True when our own ledger records placing this exchange order.

    The ownership proof of last resort on the shared Dhan account (Decision
    027). ``correlationId`` is the first proof and this is the second; a "no"
    from both means the order belongs to the user and the bot leaves it alone.

    Reads Trade directly rather than going through the reconciler's scoping
    helpers because it is called from inside the broker adapter, where the only
    question is "did WE place this id".
    """
    if not exchange_order_id:
        return False
    with session_scope() as session:
        return (
            session.execute(
                select(Trade.id).where(
                    Trade.exchange_order_id == exchange_order_id
                )
            ).first()
            is not None
        )


def _bucket_underlyings(bucket) -> set[str]:
    """The underlyings a derivative bucket's scanner sets actually name.

    Scoping the registry to these is what keeps it off the VM's memory ceiling:
    the full NSE derivative segment is 74,322 contracts and MCX another 16,298,
    against a 958MB box that has already OOM'd once (2026-08-21).

    Reads every named scanner set (Decision 026), not just the default, or a
    bucket running two sets would resolve only half its symbols.
    """
    from src.shared.scanner.engine import load_scanner_config
    from src.shared.strategy_master.loader import load_strategy_master

    names = {""}
    try:
        master = load_strategy_master(
            bucket.strategy_master_csv_path,
            bucket_trading_type=bucket.trading_type.value,
        )
        names |= {row.scanner for row in master.rows}
    except Exception:  # noqa: BLE001 — a bad CSV fails the runner, not this
        _log.warning("bucket_underlyings_master_unreadable", bucket_id=bucket.id)

    out: set[str] = set()
    for name in names:
        path = bucket.scanner_yaml_path_for(name)
        if not path.is_file():
            continue
        try:
            out |= set(load_scanner_config(path).symbols)
        except Exception:  # noqa: BLE001
            _log.warning(
                "bucket_underlyings_scanner_unreadable",
                bucket_id=bucket.id,
                scanner=name or "(default)",
            )
    return out


def _price_bands(data: object, is_dhan: bool) -> dict[str, Decimal | None] | None:
    """symbol → daily circuit band %, from Dhan's scrip master.

    None for non-Dhan accounts: crypto perpetuals have no circuit band, so
    there is nothing to clamp against and the stop percent stands as configured.
    """
    if not is_dhan or data is None:
        return None
    try:
        universe = data.universe  # type: ignore[attr-defined]
    except Exception:
        _log.warning("price_band_lookup_failed", exc_info=True)
        return None
    out: dict[str, Decimal | None] = {}
    for sym, info in universe.items():
        raw = (info or {}).get("band_pct")
        if not raw:
            continue
        try:
            out[sym] = Decimal(str(raw))
        except (ArithmeticError, ValueError):
            continue
    return out


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
    # bucket → broker product its stops must be placed under. Crypto has no
    # product dimension, so only the Dhan buckets populate this.
    stop_products: dict[str, str] = {}
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
    # Broker financing rate on the funded portion of a carried position
    # (Decision 032) — subtracted from realized P&L by the reconciler.
    carry_aprs: dict[str, Decimal] = {}
    # exchange -> the underlyings its buckets trade (Decision 037).
    derivative_underlyings: dict[str, set[str]] = {}
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
            if bucket.config.product:
                stop_products[bucket.id] = bucket.config.product
            if bucket.config.carry_interest_apr is not None:
                carry_aprs[bucket.id] = bucket.config.carry_interest_apr
            # Decision 037 — which derivative venues this process needs a
            # contract registry for. Collected per EXCHANGE rather than per
            # bucket because the registry is one catalogue per venue, and two
            # buckets on the same exchange must share it (a second copy would
            # double the parse on a 958MB VM).
            if bucket.contracts_yaml_path.is_file():
                derivative_underlyings.setdefault(
                    bucket.config.exchange, set()
                ).update(_bucket_underlyings(bucket))
    if dhan_accounts:
        try:
            # Pace charts calls under Dhan's 5 req/s Data-API cap: the
            # intraday-indian morning scan fetches ~2 calls/symbol across the
            # NIFTY-100 (Decision 029). Single-fetch paths (swing exits) only
            # eat the small per-call delay.
            # Decision 037 — attach a contract registry when any enabled
            # bucket trades derivatives. SCOPED to the underlyings those
            # buckets actually name: the full NSE segment is 74k contracts and
            # MCX another 16k, and this VM has under a gigabyte.
            #
            # One registry per process, so only ONE exchange is supported at a
            # time here. That is honest rather than limiting today (the single
            # derivative bucket is MCX), and it fails loudly below rather than
            # silently resolving half the symbols if that ever changes.
            fno_registry = None
            if derivative_underlyings:
                if len(derivative_underlyings) > 1:
                    _log.error(
                        "multiple_derivative_exchanges_unsupported",
                        exchanges=sorted(derivative_underlyings),
                    )
                exchange, underlyings = sorted(derivative_underlyings.items())[0]
                fno_registry = FnoRegistry(
                    underlyings=underlyings, exchange=exchange
                )
                _log.info(
                    "fno_registry_attached",
                    exchange=exchange,
                    underlyings=sorted(underlyings),
                )
            dhan_data = DhanData.from_settings(
                settings,
                request_delay_seconds=BOT_REQUEST_DELAY_SECONDS,
                fno=fno_registry,
            )
            for ref, ref_bucket_ids in dhan_accounts.items():
                client = DhanClient.from_settings(
                    dhan_data.resolve,
                    settings,
                    data_token_manager=dhan_data.token_manager,
                    # Decision 034 / 027: how the adapter PROVES a super order
                    # is the bot's on an account shared with the user's own
                    # trading. correlationId alone is not enough — it is
                    # unverified whether Dhan echoes it onto super-order legs,
                    # and it would not survive a restart if it did not.
                    owns_order_id=_bot_placed_order_id,
                    # Decision 036/037 — per-contract tick, lot and freeze
                    # quantity. None for a cash-only process, which keeps every
                    # equity bucket on its historical constants.
                    contract_spec=(
                        dhan_data.contract_spec if fno_registry else None
                    ),
                )
                # Reachability probe (one authed GET). Dhan's SANDBOX edge
                # blocks datacenter IPs with a bodyless 403 (confirmed from the
                # GCP VM 2026-07-12: sandbox 403, live 401) — surface that here
                # as a clean init failure (bucket skipped + one alert) instead
                # of breaker/stop-sweep ERROR spam every tick against an
                # unreachable host.
                #
                # But it must only fail fast on a PERMANENT fault. On 2026-08-10
                # a deploy restarted the bot while Dhan's 2-minute mint cooldown
                # was still running, so the probe was served a rejected token,
                # got DH-906, and disabled the whole account for the process —
                # which left no enabled buckets, and the bot exited. A token
                # eviction self-heals within ~2 minutes; retrying across that
                # window is the difference between a blip and an outage.
                _probe_with_retry(client, ref)
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
                    carry_interest_apr={
                        b: r for b, r in carry_aprs.items() if b in ref_bucket_ids
                    },
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
        # WHY the exit code matters. systemd runs this with Restart=on-failure,
        # so returning normally here means the bot stays DOWN until a human
        # notices — which is what happened on 2026-08-10: a transient token
        # state failed the Dhan probe, both Indian buckets were skipped, this
        # branch returned 0, and the service sat inactive with NRestarts=0.
        #
        # An empty buckets.yaml is a legitimate configuration and should still
        # exit cleanly. Buckets that are ENABLED but lost their account to an
        # init failure are not a configuration — they are a failure, and the
        # right response is to die loudly so systemd retries in RestartSec.
        enabled_ids = [b.id for b in all_buckets if b.config.enabled]
        if enabled_ids:
            _log.error("no_runners_despite_enabled_buckets", enabled=enabled_ids)
            send_alert(
                f"[bot] {len(enabled_ids)} bucket(s) enabled but NONE could "
                f"start ({', '.join(enabled_ids)}) — exiting non-zero so "
                f"systemd retries. Usually an account init failure above."
            )
            raise SystemExit(1)
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
    # Dhan single-session tokens get evicted whenever a second session is
    # created for the account (the user logs into the Dhan app/web, a linked
    # platform, or the depth recorder). The bot self-heals once Dhan's "one
    # token / 2 min" cooldown clears, so a DH-906 that lasts under ~2 min is
    # noise. Page only if it PERSISTS past the cooldown (genuinely stuck creds
    # / TOTP / IP block). Every non-token failure pages immediately as before.
    def _alert_safety_failure(key: str, exc: Exception, message: str) -> None:
        if is_invalid_token_error(exc):
            note_sustained_failure(
                key,
                f"{message} (Dhan token still invalid after "
                f"{int(_TOKEN_GRACE_SECONDS // 60)}m — not self-healing)",
                grace_seconds=_TOKEN_GRACE_SECONDS,
            )
        elif is_transient_upstream_error(exc):
            note_sustained_failure(
                key,
                f"{message} (Dhan unreachable/5xx for "
                f"{int(_UPSTREAM_GRACE_SECONDS // 60)}m — not a blip)",
                grace_seconds=_UPSTREAM_GRACE_SECONDS,
            )
        else:
            # Everything else pages on the first failure, by design: a 4xx, a
            # bug, or a bad config will not fix itself by being waited on.
            send_alert_dedup(key, message)

    def _note_safety_ok(key: str, message: str) -> None:
        # Ping once if a sustained token episode had paged, then re-arm both
        # channels so the next real failure alerts immediately.
        note_sustained_recovery(key, message)
        reset_alert_dedup(key)

    def _check_archive() -> None:
        """Page when the Drive archive falls behind. Never raises.

        THE ARCHIVE RUNS ON RAILWAY, SO THIS CANNOT. ``_nightly_export`` alerts
        on a failed upload, but only if the job runs at all — a dead scheduler
        container stops the archive and every alarm about it in the same
        stroke. So the watcher lives here, on the VM, exactly mirroring the
        Railway-side heartbeat watch that exists because a dead VM must not
        silence its own watchdog (Decision 020/033).

        One alarm covers all three ways the mirror dies: the scheduler stops,
        the Drive token expires, or the upload fails night after night. Each
        shows up as the same symptom — a watermark that stops moving — and
        that watermark is what gates the audit-log prune, so a stall here is
        also the reason the table starts growing.
        """
        settings_ = get_settings()
        try:
            watermark = audit_archived_through()
        except Exception:
            _log.warning("archive_watch_read_failed", exc_info=True)
            return

        today = clock.now().date()
        lag = archive_lag_days(watermark, today)
        if lag is None:
            send_alert_dedup(
                _ARCHIVE_ALERT_KEY,
                "🗄️ Audit archive has NEVER run — nothing is mirrored to "
                "Drive, and the audit-log prune is blocked (nothing is being "
                "deleted, so this is safe but not free). "
                "See docs/runbook.md → Google Drive archive.",
            )
            return
        if lag > settings_.archive_stale_days:
            _log.warning("archive_stale", watermark=str(watermark), lag_days=lag)
            send_alert_dedup(
                _ARCHIVE_ALERT_KEY,
                f"🗄️ Audit archive is STALE — last archived day is "
                f"{watermark} ({lag}d behind). The Railway scheduler may be "
                f"down, the Drive token expired, or the upload is failing. "
                f"Nothing is being deleted meanwhile (the prune is gated on "
                f"this mark), so the audit table will grow until it is fixed.",
            )
        else:
            note_alert_recovery(
                _ARCHIVE_ALERT_KEY,
                f"✅ Audit archive healthy again (through {watermark}).",
            )

    def _retire_stale_targets(ref: str) -> None:
        """Clear target legs whose cancel failed at placement (Decision 034).

        Dhan forces a TARGET_LEG onto every super order and no strategy here
        has a target, so the leg is cancelled the moment the entry is accepted.
        When that cancel fails, a live take-profit is left resting at a price no
        backtest justifies (House Rule 7) — this is the retry, riding the sweep
        that already runs every 60s inside market hours.

        Best-effort by design: a failure here means the target is still live,
        which the next tick tries again, and which never threatens the position
        the way a missing stop does.
        """
        if not settings.attached_stops_enabled:
            return  # no super orders exist ⇒ no target legs to retire
        broker = brokers[ref]
        retire = getattr(broker, "retire_stale_target_legs", None)
        if retire is None:
            return
        try:
            cleared = retire()
        except Exception:
            _log.warning("target_leg_retry_failed", account_ref=ref, exc_info=True)
            return
        if cleared:
            _log.info("target_legs_retired_late", account_ref=ref, order_ids=cleared)
            send_alert(
                f"[{ref}] retired {len(cleared)} stale super-order target "
                f"leg(s) that failed to cancel at placement"
            )

    def _sweep_stops() -> None:
        for ref, ref_bucket_ids in accounts.items():
            pcts = {b: p for b, p in stop_pcts.items() if b in ref_bucket_ids}
            if not pcts:
                continue
            # An equity venue takes no orders outside its session, so sweeping a
            # Dhan account at 22:40 can only produce rejects. On 2026-08-12 an
            # unplaceable PIIND stop was retried every ~90s all evening — 117
            # attempts in three hours, and it would have run all night, every
            # night, until the position closed. Crypto has no session and is
            # deliberately NOT gated: a 24/7 venue must be swept 24/7.
            if ref in dhan_accounts and nse_session(clock.now()) is NseSession.CLOSED:
                continue
            # Before the sweep, and outside its try: a target leg that failed
            # to cancel at placement is an armed unbacktested exit, and it must
            # not depend on the sweep succeeding to get retried.
            _retire_stale_targets(ref)
            try:
                ensure_stop_protection(
                    account_ref=ref,
                    bucket_ids=ref_bucket_ids,
                    broker=brokers[ref],
                    order_manager=order_managers[ref],
                    stop_pct_by_bucket=pcts,
                    # A stop must be placed under the SAME product as the entry
                    # it protects (2026-08-11: an INTRADAY position got an MTF
                    # stop and Dhan rejected it 116 times).
                    product_by_bucket=stop_products,
                    # A trigger outside the scrip's daily circuit band is
                    # refused at validation, so the position ends up with NO
                    # stop (PIIND, 2026-08-12). Clamp inside the band.
                    price_band_pct=_price_bands(dhan_data, ref in dhan_accounts),
                    clock=clock,
                    shared_account=ref in dhan_accounts,
                    # Decision 034 master switch. OFF ⇒ the sweep never
                    # touches the super-order endpoint at all.
                    attached_stops_enabled=settings.attached_stops_enabled,
                )
                _note_safety_ok(
                    f"stop_sweep_error:{ref}",
                    f"[bot] stop-protection sweep recovered on account {ref}",
                )
            except Exception as exc:
                _log.error(
                    "stop_protection_sweep_failed", account_ref=ref, exc_info=True
                )
                _alert_safety_failure(
                    f"stop_sweep_error:{ref}",
                    exc,
                    f"[bot] stop-protection sweep ERROR on account {ref} — see logs",
                )

    _sweep_stops()

    # ── Session invariants (per-tick process assertions) ────────────────
    # Breakers watch EQUITY; these watch PROCESS — the square-off actually
    # happening, every held position carrying a resting stop, notional inside
    # budget, the order path not rejecting, each bucket still ticking. Runs
    # AFTER the sweep so "no stop" means the sweep failed, not that it hasn't
    # run yet. A violation halts that bucket (kill switch) at most; flattening
    # stays with the breakers.
    watches: dict[str, list[BucketWatch]] = {}
    for runner in runners:
        cfg = runner.bucket.config
        indian = runner.bucket.market == Market.INDIAN
        watches.setdefault(cfg.account_ref, []).append(
            BucketWatch(
                bucket_id=runner.bucket.id,
                tick_interval_seconds=runner.tick_interval_seconds,
                # Only a same-day product must be flat at 15:15. swing-indian
                # carries MTF for days; crypto has no session at all.
                intraday=(cfg.product == "INTRADAY"),
                # INR-native equity only — see BucketWatch.notional_budget_inr.
                notional_budget_inr=(
                    cfg.capital_inr * cfg.leverage_max if indian else None
                ),
            )
        )
    invariant_thresholds = InvariantThresholds(
        squareoff_grace_minutes=settings.squareoff_grace_minutes,
        stop_coverage_sustain_ticks=settings.stop_coverage_sustain_ticks,
        notional_tolerance=Decimal(str(settings.notional_ceiling_tolerance)),
        reject_window_minutes=settings.order_reject_window_minutes,
        reject_max=settings.order_reject_max,
        bucket_stale_multiple=settings.bucket_stale_multiple,
    )
    _log.info(
        "session_invariants_ready",
        enforcing=settings.session_invariants_enforcing,
        buckets={ref: [w.bucket_id for w in ws] for ref, ws in watches.items()},
    )

    def _check_invariants() -> None:
        for ref, ref_watches in watches.items():
            # An equity account is only "stalled" during a session; overnight
            # idleness is correct behaviour, not a fault.
            live_session = (
                nse_session(clock.now()) is not NseSession.CLOSED
                if ref in dhan_accounts
                else True
            )
            try:
                results = run_session_invariants(
                    account_ref=ref,
                    watches=ref_watches,
                    broker=brokers[ref],
                    broker_name=order_managers[ref].broker_name,
                    thresholds=invariant_thresholds,
                    clock=clock,
                    shared_account=ref in dhan_accounts,
                    check_liveness=live_session,
                    attached_stops_enabled=settings.attached_stops_enabled,
                )
                enforce_session_invariants(
                    results,
                    clock=clock,
                    enforcing=settings.session_invariants_enforcing,
                )
                _note_safety_ok(
                    f"invariant_error:{ref}",
                    f"[bot] session invariants recovered on account {ref}",
                )
            except Exception as exc:
                _log.error(
                    "session_invariants_failed", account_ref=ref, exc_info=True
                )
                _alert_safety_failure(
                    f"invariant_error:{ref}",
                    exc,
                    f"[bot] session invariant check ERROR on account {ref} "
                    f"— see logs",
                )

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
                _note_safety_ok(
                    f"breaker_error:{ref}",
                    f"[bot] breaker enforcement recovered on account {ref}",
                )
            except Exception as exc:
                _log.error(
                    "breaker_enforcement_error", account_ref=ref, exc_info=True
                )
                _alert_safety_failure(
                    f"breaker_error:{ref}",
                    exc,
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
                # Per-bucket liveness. The process heartbeat below keeps
                # beating for the OTHER buckets, so only this row can show
                # that this bucket's pipeline is still completing passes.
                beat(bucket_service(runner.bucket.id), clock)
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

        # Assert the session is behaving, now that stops have been swept.
        _check_invariants()

        # Dead-man's switch: certify this tick completed. The Railway
        # scheduler pages if this row goes stale (heartbeat_stale_seconds).
        beat(SERVICE_BOT_WORKER, clock)

        global _tick_count
        _tick_count += 1
        # Hourly, and once on the first tick so a restart reports immediately
        # rather than waiting an hour to notice a stalled archive.
        if _tick_count == 1 or _tick_count % _ARCHIVE_CHECK_EVERY_TICKS == 0:
            _check_archive()
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
