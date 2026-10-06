"""lookup_cache for short-lived external lookups (news headlines, searches); x_profile_history
index; trend_term views column.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """create table lookup_cache (
             key text primary key, value jsonb not null,
             fetched_at timestamptz not null default now())"""
    )
    op.execute("alter table trend_term add column views bigint")
    op.execute("alter table image add column ocr_conf real[]")


def downgrade() -> None:
    op.execute("alter table image drop column ocr_conf")
    op.execute("alter table trend_term drop column views")
    op.execute("drop table lookup_cache")
