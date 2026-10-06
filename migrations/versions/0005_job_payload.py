"""job.payload (jsonb) for callback jobs; api_usage already exists.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table job add column payload jsonb")
    op.execute("alter table job add column callback_url text")


def downgrade() -> None:
    op.execute("alter table job drop column callback_url")
    op.execute("alter table job drop column payload")
