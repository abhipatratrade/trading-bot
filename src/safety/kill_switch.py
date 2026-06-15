"""
Kill switch — the last-resort halt mechanism.

Checked every loop iteration before any trading logic runs.
Can be flipped globally or per-strategy from the dashboard
without a redeploy (House Rule #4).
"""

from __future__ import annotations

from sqlalchemy import select

from src.core.alerts import send_alert
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    KillSwitch,
    KillSwitchScope,
)

_log = get_logger("safety.kill_switch")


def is_engaged(strategy_id: str | None = None) -> bool:
    """Return True if the global kill switch or the given strategy's is on."""
    with session_scope() as session:
        global_on = session.execute(
            select(KillSwitch).where(
                KillSwitch.scope == KillSwitchScope.GLOBAL,
                KillSwitch.engaged.is_(True),
            )
        ).scalar_one_or_none()
        if global_on:
            return True

        if strategy_id is None:
            return False

        strat_on = session.execute(
            select(KillSwitch).where(
                KillSwitch.scope == KillSwitchScope.STRATEGY,
                KillSwitch.strategy_id == strategy_id,
                KillSwitch.engaged.is_(True),
            )
        ).scalar_one_or_none()
        return strat_on is not None


def engage(
    reason: str,
    *,
    strategy_id: str | None = None,
    engaged_by: str = "system",
    clock: Clock | None = None,
) -> None:
    """Flip the kill switch ON.  Creates the row if it doesn't exist."""
    clk = clock or RealClock()
    scope = KillSwitchScope.STRATEGY if strategy_id else KillSwitchScope.GLOBAL

    with session_scope() as session:
        row = session.execute(
            select(KillSwitch).where(
                KillSwitch.scope == scope,
                KillSwitch.strategy_id == strategy_id,
            )
        ).scalar_one_or_none()

        if row is None:
            row = KillSwitch(
                scope=scope,
                strategy_id=strategy_id,
                engaged=True,
                reason=reason,
                engaged_by=engaged_by,
                engaged_at=clk.now(),
            )
            session.add(row)
        else:
            row.engaged = True
            row.reason = reason
            row.engaged_by = engaged_by
            row.engaged_at = clk.now()

        session.add(
            AuditLog(
                strategy_id=strategy_id,
                event_type=AuditEventType.KILL_SWITCH_FLIPPED,
                message=f"Kill switch ENGAGED ({scope.value}): {reason}",
                payload={
                    "scope": scope.value,
                    "strategy_id": strategy_id,
                    "engaged_by": engaged_by,
                    "reason": reason,
                },
            )
        )

    _log.warning(
        "kill_switch_engaged",
        scope=scope.value,
        strategy_id=strategy_id,
        reason=reason,
        engaged_by=engaged_by,
    )
    send_alert(
        f"KILL SWITCH ENGAGED ({scope.value}"
        f"{f', {strategy_id}' if strategy_id else ''})\n"
        f"By: {engaged_by}\nReason: {reason}"
    )


def disengage(
    *,
    strategy_id: str | None = None,
    disengaged_by: str = "manual",
) -> None:
    """Flip the kill switch OFF."""
    scope = KillSwitchScope.STRATEGY if strategy_id else KillSwitchScope.GLOBAL

    with session_scope() as session:
        row = session.execute(
            select(KillSwitch).where(
                KillSwitch.scope == scope,
                KillSwitch.strategy_id == strategy_id,
            )
        ).scalar_one_or_none()

        if row is None:
            return

        row.engaged = False
        row.reason = None
        row.engaged_by = None
        row.engaged_at = None

        session.add(
            AuditLog(
                strategy_id=strategy_id,
                event_type=AuditEventType.KILL_SWITCH_FLIPPED,
                message=f"Kill switch DISENGAGED ({scope.value})",
                payload={
                    "scope": scope.value,
                    "strategy_id": strategy_id,
                    "disengaged_by": disengaged_by,
                },
            )
        )

    _log.info(
        "kill_switch_disengaged",
        scope=scope.value,
        strategy_id=strategy_id,
        disengaged_by=disengaged_by,
    )
    send_alert(
        f"Kill switch DISENGAGED ({scope.value}"
        f"{f', {strategy_id}' if strategy_id else ''})\nBy: {disengaged_by}"
    )
