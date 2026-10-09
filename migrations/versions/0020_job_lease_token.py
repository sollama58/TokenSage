"""job.lease_token: who holds a running job's lease. claim() sets a fresh token and
complete/fail/renew_lease/defer/release only touch the row when the caller still holds it,
so a worker whose lease expired (and whose job another worker re-claimed) can no longer
overwrite the live run's status.

token_metadata.image_error / image_attempts / image_retry_at: a logo whose download failed
while the metadata itself was fine is tried again on a backoff instead of never (the
caveat is kept until it arrives).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-08
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table job add column lease_token text")
    op.execute("alter table token_metadata add column image_error text")
    op.execute("alter table token_metadata add column image_attempts int not null default 0")
    op.execute("alter table token_metadata add column image_retry_at timestamptz")


def downgrade() -> None:
    op.execute("alter table token_metadata drop column image_retry_at")
    op.execute("alter table token_metadata drop column image_attempts")
    op.execute("alter table token_metadata drop column image_error")
    op.execute("alter table job drop column lease_token")
