"""top_volume: the day's most-traded pump.fun tokens (GeckoTerminal), one snapshot per day,
for the current-meta signal (guide §5.5).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-07
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """create table top_volume (
             day date not null,
             rank int not null,
             mint text not null,
             name text,
             symbol text,
             volume_usd double precision,
             dex text,
             fetched_at timestamptz not null default now(),
             primary key (day, mint)
           )"""
    )


def downgrade() -> None:
    op.execute("drop table if exists top_volume")
