"""dhan_token table — one shared single-session access token per client id

Dhan keeps a single active token per client id (a new mint evicts the prior
token server-side). The bot and the separate-VM depth recorder both need a
live token; without a shared store they evict each other ~once a day (the
2026-07-23 ping-pong). The routine minter (the bot) writes its fresh token
here; every other process reads it instead of minting a competitor.

Revision ID: 0010_dhan_token
Revises: 0009_intraday_indian
Create Date: 2026-07-23

(Revision id kept <= 32 chars to fit alembic_version.version_num VARCHAR(32).)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_dhan_token"
down_revision: Union[str, None] = "0009_intraday_indian"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dhan_token",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("client_id", sa.String(length=32), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column(
            "minted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("minted_by", sa.String(length=32), nullable=False),
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
        sa.UniqueConstraint("client_id", name="uq_dhan_token_client"),
    )


def downgrade() -> None:
    op.drop_table("dhan_token")
