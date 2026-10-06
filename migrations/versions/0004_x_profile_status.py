"""x_profile.status (ok | suspended | not_found).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table x_profile add column status text")
    op.execute("create index x_profile_handle_idx on x_profile (lower(handle))")


def downgrade() -> None:
    op.execute("drop index if exists x_profile_handle_idx")
    op.execute("alter table x_profile drop column status")
