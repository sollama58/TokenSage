"""x_media: perceptual hashes of X post media / profile images, cached by URL.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """create table x_media (
             url text primary key,
             status text not null,          -- ok | failed
             phash bigint, phash_mirror bigint, dhash bigint,
             error text,
             fetched_at timestamptz not null default now()
           )"""
    )


def downgrade() -> None:
    op.execute("drop table x_media")
