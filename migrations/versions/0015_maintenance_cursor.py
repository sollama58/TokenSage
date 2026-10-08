"""maintenance_cursor: how far the hourly analysis prune has got through the table, so each
run picks up where the last stopped instead of rescanning a window (a rules bump re-analyses
every cached coin at once, and a window then covers most of the table).

`analysis_new` walks new analyses from deploy time on (a new version can push an old one
out of the newest 3); `analysis_aged` walks analyses as they pass 30 days, from the oldest,
so the existing backlog is pruned too, a bounded slice per run.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-08
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """create table maintenance_cursor (
             name text primary key,
             at timestamptz not null
           )"""
    )
    op.execute(
        """insert into maintenance_cursor (name, at)
           values ('analysis_new', now()),
                  ('analysis_aged', coalesce((select min(created_at) from analysis)
                                             - interval '1 microsecond', now()))"""
    )


def downgrade() -> None:
    op.execute("drop table if exists maintenance_cursor")
