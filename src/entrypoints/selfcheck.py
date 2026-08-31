"""
Pre-restart selfcheck — the last gate before ``deploy.sh`` restarts the bot.

Runs with the NEWLY-pulled code + the VM's real env, and must exit 0 before
the (old, working) bot-worker process is restarted. Catches the "deploys, then
crashes at boot" class: bad env/settings, broken bucket configs, unreachable
DB, and broker clients that cannot be BUILT.

Still NO broker/network probes — a Dhan sandbox edge-block or a flat network
must not block a deploy, because those are fail-soft inside ``run_bot``. But
*constructing* the adapters touches nothing: ``DhanData.from_settings`` and
``DhanClient.from_settings`` only assemble a token manager and an
``httpx.Client``, and the universe/scrip master loads lazily on first use. So
the wiring is checkable offline, and that gap is what let 2026-08-30 through:
``run_bot`` passed ``contract_spec=`` to a ``from_settings`` that did not
accept it, this file logged ``selfcheck_ok``, deploy.sh restarted on it, and
the bot crash-looped for 20 hours. The check below would have failed that
deploy in under a second.

Usage:  python -m src.entrypoints.selfcheck   (exit 0 = safe to restart)
"""

from __future__ import annotations

import sys

from sqlalchemy import text


def main() -> int:
    # Imports inside main so an ImportError is reported as a failure, not a
    # stack trace before logging exists.
    try:
        from src.core.config import get_settings
        from src.core.db import session_scope
        from src.core.logging import configure_logging, get_logger
        from src.shared.allocator.sizer import load_allocator_config
        from src.shared.bucket import load_buckets
        from src.shared.regime.brain import load_regime_config
        from src.shared.scanner.engine import load_scanner_config
        from src.shared.strategy_loader import discover_strategies
        from src.shared.strategy_master.loader import load_strategy_master
    except Exception as exc:  # noqa: BLE001
        print(f"SELFCHECK FAIL: import error: {exc}", file=sys.stderr)
        return 1

    configure_logging()
    log = get_logger("entrypoints.selfcheck")

    try:
        settings = get_settings()  # validates TRADING_MODE + mode credentials
    except Exception:
        log.error("selfcheck_settings_failed", exc_info=True)
        return 1

    try:
        buckets = load_buckets()
        for b in buckets:
            if not b.config.enabled:
                continue
            master = load_strategy_master(
                b.strategy_master_csv_path,
                bucket_trading_type=b.trading_type.value,
            )
            load_regime_config(b.regime_yaml_path)
            for name in {""} | {row.scanner for row in master.rows}:
                load_scanner_config(b.scanner_yaml_path_for(name))
                load_allocator_config(b.allocator_yaml_path_for(name))
            discover_strategies(b.strategies_folder)
    except Exception:
        log.error("selfcheck_bucket_configs_failed", exc_info=True)
        return 1

    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        log.error("selfcheck_db_unreachable", exc_info=True)
        return 1

    # Broker WIRING (not reachability). Mirrors run_bot's construction, so a
    # signature that drifts apart from its caller fails the deploy here rather
    # than in a systemd restart loop. No request is made.
    if any(b.config.enabled and b.market.value == "indian" for b in buckets):
        try:
            from src.brokers.dhan.client import DhanClient
            from src.data_sources.dhan import DhanData

            dhan_data = DhanData.from_settings(settings, request_delay_seconds=0.0)
            DhanClient.from_settings(
                dhan_data.resolve,
                settings,
                data_token_manager=dhan_data.token_manager,
                owns_order_id=lambda _oid: False,
                contract_spec=None,
            )
        except (TypeError, AttributeError):
            # A signature or attribute that drifted from its caller. Always a
            # code bug, never the environment, and always fatal at boot.
            log.error("selfcheck_dhan_wiring_failed", exc_info=True)
            return 1
        except Exception:
            # Environment, not wiring — absent Dhan creds on this host, a DB
            # blip reaching the shared token row. run_bot is fail-soft for
            # these (bucket skipped, other buckets run), so failing the deploy
            # would be an escalation. Worse, it would also block deploying the
            # very fix: a credential fault would wedge the deploy pipeline
            # shut. Warn and let it through.
            log.warning("selfcheck_dhan_construct_skipped", exc_info=True)

    log.info(
        "selfcheck_ok",
        trading_mode=settings.trading_mode.value,
        enabled_buckets=[b.id for b in buckets if b.config.enabled],
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
