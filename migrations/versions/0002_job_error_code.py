"""job.error_code: a machine-readable reason for a failed job (maps to API error codes).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table job add column error_code text")
    op.execute("alter table token_metadata add column image_content_key text")
    op.execute(
        "alter table image add column sha256 text, add column mime text, add column bytes int"
    )


def downgrade() -> None:
    op.execute("alter table image drop column sha256, drop column mime, drop column bytes")
    op.execute("alter table token_metadata drop column image_content_key")
    op.execute("alter table job drop column error_code")
