"""Retention for the token family (database audit 2026-10-16, I1): until now token,
token_metadata, token_read, token_market, analysis (its newest versions), image, x_* and the
pair_token / fee_recipient caches grew a row per read for ever, toward the 15 GB default
disk. The hourly maintenance now prunes them (tokensage/jobs/maintenance.py, _prune_tokens).

`token_detail` is that prune's cursor through token.created_at: it walks tokens as they pass
the detail horizon and drops their heavy children, a bounded slice per run, starting from the
oldest. `token_undated_idx` serves the tokens whose launch time could never be determined
(created_at null): they are outside every meta and copycat count, so they go whole on the
detail horizon by first_seen_at, and without the index that would be a table scan every hour.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """insert into maintenance_cursor (name, at)
           values ('token_detail', coalesce((select min(created_at) from token)
                                            - interval '1 microsecond', now()))
           on conflict (name) do nothing"""
    )
    # Built concurrently: a plain CREATE INDEX would hold token against the analyzer's
    # inserts for the whole build.
    with op.get_context().autocommit_block():
        op.execute("drop index concurrently if exists token_undated_idx")
        op.execute(
            """create index concurrently token_undated_idx on token (first_seen_at)
               where created_at is null"""
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("drop index concurrently if exists token_undated_idx")
    op.execute("delete from maintenance_cursor where name = 'token_detail'")
