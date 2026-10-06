"""Initial schema (PROJECT_GUIDE.md §7).

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = """
create table token (
  mint text primary key,
  name text, symbol text, uri text,
  creator text, bonding_curve text, token_program text, quote_mint text,
  is_pumpfun boolean not null default false,
  is_mayhem boolean,
  created_at timestamptz,
  created_at_source text,
  launcher text,
  seen_by text[] not null default '{}',
  first_seen_at timestamptz not null default now()
);
create index token_created_at_idx on token (created_at desc);
create index token_symbol_idx on token (upper(symbol));
create index token_creator_idx on token (creator);

create table token_metadata (
  mint text primary key references token on delete cascade,
  status text not null,
  content_key text,
  description text, image_url text, twitter text, telegram text, website text,
  raw jsonb,
  attempts int not null default 0, next_retry_at timestamptz,
  fetched_at timestamptz
);

create table token_market (
  mint text primary key references token on delete cascade,
  complete boolean, curve_progress real, usd_market_cap double precision,
  reply_count int, hidden boolean, is_banned boolean,
  updated_at timestamptz
);

create table image (
  content_key text primary key,
  phash bigint, dhash bigint, phash_mirror bigint, pdq bytea,
  ocr text[], palette text[], labels jsonb,
  width int, height int, animated boolean, analyzed_at timestamptz
);
create index image_phash_idx on image (phash);

create table x_ref (
  mint text primary key references token on delete cascade,
  kind text, tweet_id text, community_id text, handle text, user_id text,
  object_time timestamptz
);
create index x_ref_tweet_idx on x_ref (tweet_id);
create index x_ref_handle_idx on x_ref (lower(handle));
create index x_ref_community_idx on x_ref (community_id);

create table x_tweet (
  tweet_id text primary key, first_snapshot jsonb, latest jsonb,
  status text, source text, fetched_at timestamptz
);
create table x_profile (
  user_id text primary key, handle text, snapshot jsonb,
  source text, fetched_at timestamptz
);
create table x_profile_history (
  user_id text, handle text, followers int, seen_at timestamptz not null default now()
);
create index x_profile_history_user_idx on x_profile_history (user_id, seen_at desc);

create table analysis (
  mint text references token on delete cascade,
  version int not null,
  depth text not null,
  doc jsonb not null,
  referent text, categories text[], flags text[],
  created_at timestamptz not null default now(),
  primary key (mint, version)
);
create index analysis_categories_idx on analysis using gin (categories);
create index analysis_mint_depth_idx on analysis (mint, depth, version desc);

create table known_coin (
  id text primary key, chain text, mint text, name text, symbol text,
  aliases text[], lore text, categories text[], logo_phash bigint,
  source text, updated_at timestamptz
);
create index known_coin_symbol_idx on known_coin (upper(symbol));

create table entity (
  id text primary key, label text, aliases text[], kind text, description text,
  source text, popularity real
);

create table trend_term (
  term text, source text, score real, spike real, first_seen date, day date,
  primary key (term, source, day)
);

create table job (
  id bigserial primary key,
  kind text not null,
  mint text, depth text,
  priority int not null default 100,
  status text not null default 'pending',
  run_after timestamptz not null default now(),
  attempts int not null default 0,
  locked_until timestamptz,
  last_error text,
  result_version int,
  requested_by text,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
create unique index job_single_flight_idx on job (kind, mint, depth)
  where status in ('pending', 'running');
create index job_pending_idx on job (priority, run_after) where status = 'pending';

create table api_key (
  name text primary key,
  key_sha256 text unique not null,
  rate_per_min int not null default 60,
  full_per_day int not null default 2000,
  refresh_per_day int not null default 200,
  callback_secret text,
  created_at timestamptz not null default now(),
  revoked_at timestamptz
);
create table api_usage (
  key_name text, day date, requests int not null default 0,
  full_calls int not null default 0, refreshes int not null default 0,
  primary key (key_name, day)
);

create table feed_state (feed text primary key, high_water jsonb, updated_at timestamptz);
create table source_health (
  source text primary key, state text, failures int not null default 0,
  open_until timestamptz, calls_today int not null default 0,
  spend_today_usd numeric not null default 0
);
"""

DOWNGRADE = """
drop table if exists source_health, feed_state, api_usage, api_key, job, trend_term, entity,
  known_coin, analysis, x_profile_history, x_profile, x_tweet, x_ref, image, token_market,
  token_metadata, token cascade;
"""


def upgrade() -> None:
    for stmt in UPGRADE.split(";"):
        if stmt.strip():
            op.execute(stmt)


def downgrade() -> None:
    op.execute(DOWNGRADE)
