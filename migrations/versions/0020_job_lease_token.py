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
    # Each ALTER commits on its own, so the ACCESS EXCLUSIVE lock on job (every claim and
    # enqueue) is released before token_metadata is locked, and a wait behind a long reader
    # fails the deploy after 10 s instead of stalling the queue (as 0017 does). Every
    # statement is idempotent, so a rerun continues where it stopped.
    with op.get_context().autocommit_block():
        op.execute("set lock_timeout = '10s'")
        op.execute("alter table job add column if not exists lease_token text")
        op.execute("alter table token_metadata add column if not exists image_error text")
        op.execute(
            "alter table token_metadata add column if not exists image_attempts int"
            " not null default 0"
        )
        op.execute("alter table token_metadata add column if not exists image_retry_at timestamptz")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("set lock_timeout = '10s'")
        op.execute("alter table token_metadata drop column if exists image_retry_at")
        op.execute("alter table token_metadata drop column if exists image_attempts")
        op.execute("alter table token_metadata drop column if exists image_error")
        op.execute("alter table job drop column if exists lease_token")
