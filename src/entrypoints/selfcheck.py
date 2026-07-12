"""
Pre-restart selfcheck — the last gate before ``deploy.sh`` restarts the bot.

Runs with the NEWLY-pulled code + the VM's real env, and must exit 0 before
the (old, working) bot-worker process is restarted. Catches the "deploys, then
crashes at boot" class: bad env/settings, broken bucket configs, unreachable
DB. Deliberately NO broker/network probes — those are fail-soft inside
``run_bot`` itself (e.g. the Dhan sandbox edge-block must not block a deploy).

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

    log.info(
        "selfcheck_ok",
        trading_mode=settings.trading_mode.value,
        enabled_buckets=[b.id for b in buckets if b.config.enabled],
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
