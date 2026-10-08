"""job_analyze_mint_idx: the newest analyze job for a (mint, depth), which the API reads on
every cache miss to see whether the coin just failed (service._recent_failure). Without it
that lookup scanned the whole job table (7 days of jobs) on every miss. Built concurrently
so the API and workers keep writing jobs while it builds.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-08
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # a failed concurrent build leaves an invalid index behind: start clean
        op.execute("drop index concurrently if exists job_analyze_mint_idx")
        op.execute(
            """create index concurrently job_analyze_mint_idx on job (mint, depth, id desc)
               where kind = 'analyze'"""
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("drop index concurrently if exists job_analyze_mint_idx")
