"""pair_token.is_pumpfun: whether the token a coin is paired against is itself a pump.fun
coin. pump.fun now lets a coin pair with any other pump.fun coin, not just SOL, stablecoins
and a few majors; the summary names such a pair token as a pump.fun coin.

Null on rows cached before this column: those are read again on their next lookup.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-08
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table pair_token add column is_pumpfun boolean")


def downgrade() -> None:
    op.execute("alter table pair_token drop column is_pumpfun")
