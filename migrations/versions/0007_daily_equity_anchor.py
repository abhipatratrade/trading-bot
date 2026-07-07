"""daily_equity_anchor table — start-of-day equity per sub-account

Decision 023: the daily-drawdown breaker measures realized + unrealized
loss against a fixed start-of-day (UTC) equity anchor instead of the old
instantaneous unrealized-vs-current-equity check. The first breaker pass
of each UTC day inserts the row; restarts within the day reuse it.

Revision ID: 0007_daily_equity_anchor
Revises: 0006_regime_signal_reject
Create Date: 2026-07-07

(Revision id kept <= 32 chars to fit alembic_version.version_num VARCHAR(32).)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_daily_equity_anchor"
down_revision: Union[str, None] = "0006_regime_signal_reject"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_equity_anchor",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_ref", sa.String(length=64), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("equity", sa.Numeric(28, 12), nullable=False),
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
        sa.UniqueConstraint(
            "account_ref", "date", name="uq_equity_anchor_account_date"
        ),
    )


def downgrade() -> None:
    op.drop_table("daily_equity_anchor")
