"""Each service's report of which expected env vars it has set (tokensage/envreport.py), for
TrenchScanner's Admin > Environment. Written by the analyzer and the crons when they start;
the api reports live.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-23
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """create table if not exists service_env (
             service text primary key,
             vars jsonb not null,
             commit text,
             reported_at timestamptz not null default now()
           )"""
    )


def downgrade() -> None:
    op.execute("drop table if exists service_env")
