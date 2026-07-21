"""seed bucket_state for the intraday-indian bucket (Decision 029)

Seventh bucket, amending Decision 013's fixed six. NIFTY-100 gap-down
reversal — long-only intraday fade, holdout-validated PF 1.68.

Capital is Rs 1,00,000 rather than the house Rs 50,000: at the frozen
config's 20% per-symbol cap and 5x MIS that yields Rs 1L notional per
trade, the size the backtest was validated at (the edge does not clear
the cost floor at Rs 10k, and thins at Rs 50k).

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
        "VALUES ('intraday-indian', 100000, 100000, 0, false) "
        "ON CONFLICT (bucket_id) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM bucket_state WHERE bucket_id = 'intraday-indian'")
