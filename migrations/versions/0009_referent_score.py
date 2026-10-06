"""analysis.referent_score: the referent's confidence, so recall (how often the engine
resolves what a coin refers to) can be measured with one query.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table analysis add column referent_score real")
    op.execute("create index analysis_created_at_idx on analysis (created_at desc)")


def downgrade() -> None:
    op.execute("drop index if exists analysis_created_at_idx")
    op.execute("alter table analysis drop column referent_score")
