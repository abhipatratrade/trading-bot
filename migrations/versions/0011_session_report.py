"""session_report table — one end-of-day postmortem per trading date

Phase 7a Tier 3 (Decision 033). The scheduler builds the report after the
close and stores it here; the dashboard renders it at /journal and
``scripts/export_journal.py`` materialises it into docs/journal/*.md for git.
Postgres is the store because the Railway scheduler container is ephemeral and
has no git credentials.

Revision ID: 0011_session_report
Revises: 0010_dhan_token
Create Date: 2026-07-28

(Revision id kept <= 32 chars to fit alembic_version.version_num VARCHAR(32).)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011_session_report"
down_revision: Union[str, None] = "0010_dhan_token"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_report",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # One report per date; a re-run UPSERTs rather than accumulating.
        sa.UniqueConstraint("session_date", name="uq_session_report_date"),
    )


def downgrade() -> None:
    op.drop_table("session_report")
