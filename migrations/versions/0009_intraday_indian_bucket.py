"""seed bucket_state for the intraday-indian bucket (Decision 029)

Seventh bucket, amending Decision 013's fixed six. NIFTY-100 gap-down
reversal — long-only intraday fade, holdout-validated PF 1.68.

Capital is the house Rs 50,000 (user decision 2026-07-21). At the frozen
config's 20% per-symbol cap and 5x that is 5 slots of Rs 50k notional,
vs the Rs 1L the backtest used; the extra cost is ~0.02% round-trip,
well clear of the Rs 10k cliff where brokerage becomes 0.2%/leg.

Seeded with enabled=false to match buckets.yaml — the bucket is built
dark and switched on by hand after review.

Revision ID: 0009_intraday_indian
Revises: 0008_heartbeat
Create Date: 2026-07-21

(Revision id kept <= 32 chars to fit alembic_version.version_num VARCHAR(32).)
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_intraday_indian"
down_revision: Union[str, None] = "0008_heartbeat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: re-running on a DB that already has the row is a no-op, so
    # this is safe to replay against prod after a partial deploy.
    op.execute(
        "INSERT INTO bucket_state "
        "(bucket_id, capital_inr, available_balance_inr, locked_margin_inr, enabled) "
        "VALUES ('intraday-indian', 50000, 50000, 0, false) "
        "ON CONFLICT (bucket_id) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM bucket_state WHERE bucket_id = 'intraday-indian'")
