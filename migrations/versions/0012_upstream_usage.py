"""upstream_usage: outbound calls per hour, source and method (calls, errors, 429s,
latency, Helius credits) for the admin panel; job.started_at to split queue wait from
processing time; source_health.updated_at so recovered sources age out.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-07
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """create table upstream_usage (
             hour timestamptz not null,
             source text not null,
             method text not null,
             calls int not null default 0,
             errors int not null default 0,
             rate_limited int not null default 0,
             credits bigint not null default 0,
             ms_total bigint not null default 0,
             ms_max int not null default 0,
             peak_rps int not null default 0,
             primary key (hour, source, method)
           )"""
    )
    op.execute("create index upstream_usage_source_hour_idx on upstream_usage (source, hour)")
    op.execute("alter table job add column started_at timestamptz")
    op.execute("create index job_finished_at_idx on job (finished_at) where finished_at is not null")
    op.execute("alter table source_health add column updated_at timestamptz not null default now()")


def downgrade() -> None:
    op.execute("alter table source_health drop column updated_at")
    op.execute("drop index if exists job_finished_at_idx")
    op.execute("alter table job drop column started_at")
    op.execute("drop table if exists upstream_usage")
