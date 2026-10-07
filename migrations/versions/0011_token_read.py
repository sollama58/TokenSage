"""token_read: each coin's latest read (referent key and categories) by launch time, for
the referent wave and category wave counts (guide §5.5); indexes on the compacted token name
(same-name lookups) and its trigrams (current-meta word counts).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-07
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """create table token_read (
             mint text primary key references token on delete cascade,
             launched_at timestamptz not null,
             referent_key text,
             referent_label text,
             categories text[] not null default '{}',
             updated_at timestamptz not null default now()
           )"""
    )
    op.execute("create index token_read_referent_idx on token_read (referent_key, launched_at)")
    op.execute("create index token_read_launched_idx on token_read (launched_at desc)")
    # the same-name lookups (analyzer._db_context) match on the compacted name: index it so
    # they stop scanning every token of the copycat window
    op.execute(
        """create index token_name_compact_idx on token
           (regexp_replace(lower(coalesce(name, '')), '[^a-z0-9]', '', 'g'))"""
    )
    # the current-meta word counts (analyzer._meta_counts) run a word regex over 90 days of
    # names; a trigram index serves it. pg_trgm ships with Postgres (Render included); if the
    # role may not create it, the counts keep working without the index.
    op.execute(
        """do $$ begin
             create extension if not exists pg_trgm;
           exception when others then
             raise notice 'pg_trgm unavailable (%), token_name_trgm_idx skipped', sqlerrm;
           end $$"""
    )
    op.execute(
        """do $$ begin
             if exists (select 1 from pg_extension where extname = 'pg_trgm') then
               create index if not exists token_name_trgm_idx on token
                 using gin (lower(coalesce(name, '')) gin_trgm_ops);
             end if;
           end $$"""
    )


def downgrade() -> None:
    op.execute("drop index if exists token_name_trgm_idx")
    op.execute("drop index if exists token_name_compact_idx")
    op.execute("drop table if exists token_read")
