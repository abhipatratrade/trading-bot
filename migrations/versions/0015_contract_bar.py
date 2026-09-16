"""contract_bar — a cache of completed derivative bars that outlives the contract

Phase 12b. commodity-indian's CCI signal moves from "the execution contract's
own 90 days" to a continuous front-month-by-expiry series, which is what the
validated run actually used (TradingView ``NATGASMINI1!``). The pre-roll leg
of that series comes from a contract Dhan's scrip master drops on expiry, so
its bars are fetchable only while it is alive. Every live fetch writes through
here; the expired leg is read back from here.

Cache, not state: nothing is decided from a row the bot could not have fetched.

Revision ID: 0015_contract_bar
Revises: 0014_commodity_indian
Create Date: 2026-09-16

(Revision id kept <= 32 chars to fit alembic_version.version_num VARCHAR(32).)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_contract_bar"
down_revision: Union[str, None] = "0014_commodity_indian"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "contract_bar",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("tf", sa.String(8), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(28, 12), nullable=False),
        sa.Column("high", sa.Numeric(28, 12), nullable=False),
        sa.Column("low", sa.Numeric(28, 12), nullable=False),
        sa.Column("close", sa.Numeric(28, 12), nullable=False),
        sa.Column("volume", sa.Numeric(28, 12), nullable=False, server_default="0"),
        sa.UniqueConstraint("symbol", "tf", "ts", name="uq_contract_bar"),
    )
    op.create_index("ix_contract_bar_symbol_tf", "contract_bar", ["symbol", "tf"])


def downgrade() -> None:
    op.drop_index("ix_contract_bar_symbol_tf", table_name="contract_bar")
    op.drop_table("contract_bar")
