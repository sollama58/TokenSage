"""The image trigger of 0017 reads the coins of a logo with their token_metadata rows locked
(`for share`), so a coin whose logo changes while the trigger runs is re-read at its new
logo and left alone, instead of being given the old logo's hash.

Before, the trigger's UPDATE ... FROM token_metadata re-checked a concurrently changed coin
against the token_metadata row it had already read, and wrote the old logo's hash over the
one the coin's own trigger had just set.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-08
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """create or replace function token_logo_phash_from_image() returns trigger
           language plpgsql as $$
           begin
             update token t set logo_phash = new.phash
              where t.mint in (select tm.mint from token_metadata tm
                                where tm.image_content_key = new.content_key
                                  for share)
                and t.logo_phash is distinct from new.phash;
             return null;
           end $$"""
    )


def downgrade() -> None:
    op.execute(
        """create or replace function token_logo_phash_from_image() returns trigger
           language plpgsql as $$
           begin
             update token t set logo_phash = new.phash
               from token_metadata tm
              where tm.image_content_key = new.content_key and t.mint = tm.mint
                and t.logo_phash is distinct from new.phash;
             return null;
           end $$"""
    )
