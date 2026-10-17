"""An index on job.created_at (performance audit 2026-10-17): the monitor's queue view
(/admin/v1/queue) filters jobs by created_at in three of its queries, and with no index each
was a scan of the whole 7-day job table, 13-17 s per read in production whatever the window.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-17
"""

from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Built concurrently: a plain CREATE INDEX would hold job against the claims and inserts
    # for the whole build.
    with op.get_context().autocommit_block():
        op.execute("drop index concurrently if exists job_created_at_idx")
        op.execute("create index concurrently job_created_at_idx on job (created_at)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("drop index concurrently if exists job_created_at_idx")
