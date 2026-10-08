"""analysis_recent_idx: created_at with the columns the recall summary and the admin
signals counts read, so both are answered from the index alone (no heap reads) as the
analysis table grows; replaces analysis_created_at_idx. Built concurrently so the workers
keep inserting analyses while it builds.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-08
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # a failed concurrent build leaves an invalid index behind: start clean
        op.execute("drop index concurrently if exists analysis_recent_idx")
        op.execute(
            """create index concurrently analysis_recent_idx on analysis (created_at)
               include (mint, depth, version, referent_score, referent)"""
        )
        op.execute("drop index concurrently if exists analysis_created_at_idx")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "create index concurrently if not exists analysis_created_at_idx"
            " on analysis (created_at desc)"
        )
        op.execute("drop index concurrently if exists analysis_recent_idx")
