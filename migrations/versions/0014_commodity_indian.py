"""seed bucket_state for the commodity-indian bucket (Decision 037)

Eighth bucket, and the first on a DIFFERENT EXCHANGE: MCX commodity futures
(NATGASMINI), CCI(20) 225/250 reversion on 15m. Segment MCX_COMM, session
09:00-23:30, CTT instead of STT.

Capital is Rs 5,00,000. Sizing context, because the figure looks large next to
the Rs 50k equity buckets: one NATGASMINI lot blocks Rs 9,859 of margin (probed
live 2026-08-29, 14.3% of notional), so Rs 5L is a book of a few lots rather
than a single position.

Seeded with enabled=false to match buckets.yaml. That is not the usual
build-dark-then-review: the PORT is proven (scripts/cci_gas_parity.py
reproduces 125 of 125 backtested trades) but the STRATEGY has no out-of-sample
fold, is negative on adjacent timeframes over the same months, and its edge
lives in overnight exposure the exchange-resident stop cannot cover. Enabling
it is a deliberate act with those facts in hand.

Revision ID: 0014_commodity_indian
Revises: 0013_bar_key_backfill
Create Date: 2026-08-30

(Revision id kept <= 32 chars to fit alembic_version.version_num VARCHAR(32).)
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_commodity_indian"
down_revision: Union[str, None] = "0013_bar_key_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: re-running on a DB that already has the row is a no-op, so
    # this is safe to replay against prod after a partial deploy.
    op.execute(
        "INSERT INTO bucket_state "
        "(bucket_id, capital_inr, available_balance_inr, locked_margin_inr, enabled) "
        "VALUES ('commodity-indian', 500000, 500000, 0, false) "
        "ON CONFLICT (bucket_id) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM bucket_state WHERE bucket_id = 'commodity-indian'")
