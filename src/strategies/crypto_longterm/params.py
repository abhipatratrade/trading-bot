"""
Load policy.yaml, validate, and audit-log the load to strategy_param_change.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sqlalchemy import select

from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import AuditEventType, AuditLog, StrategyParamChange
from src.strategies.crypto_longterm.schema import CryptoLongtermPolicy, load_policy

_log = get_logger("strategies.crypto_longterm.params")


def _git_sha() -> str | None:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:
        return None


def load_and_audit(path: Path | None = None) -> CryptoLongtermPolicy:
    """Load policy, record in DB, return validated config."""
    policy = load_policy(path)
    yaml_text = (path or Path(__file__).resolve().parent / "policy.yaml").read_text(
        encoding="utf-8"
    )
    git_sha = _git_sha()

    with session_scope() as session:
        prev = session.execute(
            select(StrategyParamChange)
            .where(StrategyParamChange.strategy_id == policy.strategy_id)
            .order_by(StrategyParamChange.version.desc())
            .limit(1)
        ).scalar_one_or_none()

        prior_version = prev.version if prev else None

        session.add(
            StrategyParamChange(
                strategy_id=policy.strategy_id,
                version=policy.version,
                prior_version=prior_version,
                git_sha=git_sha,
                backtest_ref=policy.backtest_ref,
                policy_yaml=yaml_text,
            )
        )
        session.add(
            AuditLog(
                strategy_id=policy.strategy_id,
                event_type=AuditEventType.PARAMS_LOADED,
                message=f"Loaded policy v{policy.version} (backtest_ref={policy.backtest_ref})",
                payload={
                    "version": policy.version,
                    "prior_version": prior_version,
                    "git_sha": git_sha,
                    "backtest_ref": policy.backtest_ref,
                },
            )
        )

    _log.info(
        "policy_loaded",
        strategy_id=policy.strategy_id,
        version=policy.version,
        backtest_ref=policy.backtest_ref,
    )
    return policy
