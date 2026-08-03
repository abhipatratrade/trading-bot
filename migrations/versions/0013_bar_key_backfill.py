"""backfill scanner bar_key — 0012's UPDATE never matched a row

0012 added ``bar_key`` WITH a server_default and then backfilled
``WHERE bar_key IS NULL``. In Postgres those two steps fight each other:
``ADD COLUMN ... DEFAULT ''`` populates every existing row immediately, so
nothing was ever NULL and the UPDATE was a no-op. All 2,342 scanner_snapshot
and 168 daily_universe rows ended up with ``''`` instead of their ISO date.

Nothing was lost — ``date`` still carries the day — but ``''`` was documented
in 0012 as meaning "written by old code inside a deploy window", and 2,510
legacy rows wearing that mark makes it useless as a signal. This restores the
intent.

Legacy swing-indian rows are approximate by necessity: they were written before
per-bin keying existed, so which of the 7 bins each came from is unrecoverable.
The ISO date is the same statement the once-a-day scanners make, and the
honest one for a row that predates the distinction.

The server_default stays. It is load-bearing for the deploy window (ops/deploy.sh
migrates before it restarts), and from here on ``''`` means only that.

Revision ID: 0013_bar_key_backfill
Revises: 0012_scanner_bar_key
Create Date: 2026-08-03

(Revision id kept <= 32 chars to fit alembic_version.version_num VARCHAR(32).)
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_bar_key_backfill"
down_revision: Union[str, None] = "0012_scanner_bar_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BACKFILL = (
    "UPDATE scanner_snapshot SET bar_key = date::text WHERE bar_key = ''",
    "UPDATE daily_universe SET bar_key = date::text WHERE bar_key = ''",
)


def upgrade() -> None:
    # Safe for the unique key: rows unique on (date, strategy_id, symbol, '')
    # stay unique on (date, strategy_id, symbol, date::text) — the fourth
    # column moves from one constant to another within each date group.
    for statement in _BACKFILL:
        op.execute(statement)


def downgrade() -> None:
    # Not reversible in any meaningful sense: '' and the ISO date are
    # indistinguishable afterwards, and restoring '' would destroy the keys
    # written by the fixed code. Deliberately a no-op.
    pass
