"""entity: categories, sitelinks and updated_at for the Wikidata gazetteer (guide §4.4).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table entity add column categories text[] not null default '{}'")
    op.execute("alter table entity add column sitelinks int")
    op.execute("alter table entity add column updated_at timestamptz")
    op.execute("create index entity_source_idx on entity (source, updated_at)")


def downgrade() -> None:
    op.execute("drop index if exists entity_source_idx")
    op.execute("alter table entity drop column updated_at")
    op.execute("alter table entity drop column sitelinks")
    op.execute("alter table entity drop column categories")
