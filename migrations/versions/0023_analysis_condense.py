"""Condensing old analyses (retention, 2026-10-17): the hourly maintenance drops the heavy
parts of every analysis past ANALYSIS_CONDENSE_DAYS (tokensage/jobs/maintenance.py,
_condense_analyses). `analysis_condense` is its cursor through analysis.created_at, walked
from the oldest row through analysis_recent_idx, a bounded slice per run.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-17
"""

from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """insert into maintenance_cursor (name, at)
           values ('analysis_condense', coalesce((select min(created_at) from analysis)
                                                 - interval '1 microsecond', now()))
           on conflict (name) do nothing"""
    )


def downgrade() -> None:
    op.execute("delete from maintenance_cursor where name = 'analysis_condense'")
