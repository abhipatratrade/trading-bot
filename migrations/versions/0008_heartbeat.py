"""heartbeat table — dead-man's switch liveness row per service

Phase 1c: the bot-worker upserts its row every tick; the Railway
scheduler pages when the row goes stale, so a dead VM/bot no longer
fails silently.

Revision ID: 0008_heartbeat
Revises: 0007_daily_equity_anchor
Create Date: 2026-07-07

(Revision id kept <= 32 chars to fit alembic_version.version_num VARCHAR(32).)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008_heartbeat"
down_revision: Union[str, None] = "0007_daily_equity_anchor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "heartbeat",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.UniqueConstraint("service", name="uq_heartbeat_service"),
    )


def downgrade() -> None:
    op.drop_table("heartbeat")
