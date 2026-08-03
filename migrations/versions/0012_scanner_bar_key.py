"""scanner bar_key + INVARIANT_VIOLATED audit value

Two independent recording fixes, both found reviewing the 2026-08-03 journal.

- ``scanner_snapshot`` and ``daily_universe`` gain ``bar_key`` and take it into
  their unique key. Both tables are written delete-then-insert scoped to
  (date, strategy_id), which is correct for a once-a-day screen and destructive
  for swing-indian's meanrev cut: it runs on every completed 1h bin, so each of
  the 7 daily passes wiped the previous one. Only the 15:16 pass survived the
  session. Backfilled with the ISO date, which is the true bar for every row
  written so far by the once-a-day scanners, and the best available answer for
  the meanrev rows (only one pass per day survived, so nothing is ambiguous).

- ``audit_event_type`` gains ``INVARIANT_VIOLATED`` — emitted by
  ``safety/session_invariants.py`` when a check fails. Until now invariants
  wrote no row at all: they logged and paged Telegram, and only reached
  Postgres if they escalated to HALT (via KILL_SWITCH_FLIPPED). The EOD journal
  is built from audit rows, so it reported "nothing tripped" for every
  violation that cleared before it halted anything. SAEnum serialises the
  UPPERCASE member name, matching the convention restored in migration 0003.

Revision ID: 0012_scanner_bar_key
Revises: 0011_session_report
Create Date: 2026-08-03

(Revision id kept <= 32 chars to fit alembic_version.version_num VARCHAR(32).)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_scanner_bar_key"
down_revision: Union[str, None] = "0011_session_report"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    # (table, unique constraint, backfill statement)
    #
    # The backfill is written out per table rather than interpolated: these are
    # the only two DDL targets this migration has, and a literal is both
    # clearer at review time and free of the SQL-construction question.
    (
        "scanner_snapshot",
        "uq_scanner_snapshot_key",
        "UPDATE scanner_snapshot SET bar_key = date::text WHERE bar_key IS NULL",
    ),
    (
        "daily_universe",
        "uq_daily_universe_key",
        "UPDATE daily_universe SET bar_key = date::text WHERE bar_key IS NULL",
    ),
)


def upgrade() -> None:
    for table, constraint, backfill in _TABLES:
        # Added nullable, backfilled, then made NOT NULL — the three-step dance
        # that lets this run against a table that already holds rows.
        #
        # BUG, fixed in 0013 rather than edited here because this already ran
        # in production: ADD COLUMN with a server_default populates every
        # existing row on the spot, so `WHERE bar_key IS NULL` below matched
        # nothing and the ISO-date backfill silently did not happen. Every
        # legacy row got '' instead. Harmless (the `date` column still carries
        # the day) but it wasted the '' sentinel, so 0013 backfills properly.
        #
        # The server_default is deliberate and PERMANENT. ops/deploy.sh applies
        # migrations BEFORE restarting the bot, so for a few seconds the OLD
        # code is still live against the NEW schema, inserting scanner rows with
        # no bar_key. Without a default those inserts raise inside a scan on a
        # live trading process. It is not in the ORM model: every code path
        # supplies the value explicitly, and '' is only ever a fingerprint of a
        # row written inside a deploy window.
        op.add_column(
            table,
            sa.Column(
                "bar_key", sa.String(32), nullable=True, server_default=sa.text("''")
            ),
        )
        op.execute(backfill)
        op.alter_column(table, "bar_key", nullable=False)
        # Widen the key. Dropping first is required: the new constraint is a
        # superset and Postgres will not replace one in place.
        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(
            constraint, table, ["date", "strategy_id", "symbol", "bar_key"]
        )

    # ADD VALUE IF NOT EXISTS is idempotent. The new value is not referenced in
    # this migration, so adding it in-transaction is safe (PG 12+).
    op.execute(
        "ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'INVARIANT_VIOLATED'"
    )


def downgrade() -> None:
    # Narrowing the key back will FAIL if any session has stored more than one
    # bin per (date, strategy_id, symbol) — which is the whole point of the
    # upgrade. Deduplicate first (keep the newest bar per symbol per day) if you
    # genuinely need to go back.
    for table, constraint, _ in _TABLES:
        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(
            constraint, table, ["date", "strategy_id", "symbol"]
        )
        op.drop_column(table, "bar_key")
    # Postgres can't drop an enum value without recreating the type; the added
    # value is harmless when unused, so we leave it (see migration 0003).
