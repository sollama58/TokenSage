"""pair_token: name / symbol of tokens that coins are paired against, cached.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """create table pair_token (
             mint text primary key,
             name text, symbol text,
             source text not null,          -- db | onchain | none
             fetched_at timestamptz not null default now()
           )"""
    )


def downgrade() -> None:
    op.execute("drop table pair_token")
