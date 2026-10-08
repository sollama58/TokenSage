"""token.logo_phash: the token's logo phash copied next to its launch time, so the logo
near-duplicate lookup reads one narrow index range (token_logo_idx, index-only) instead of
joining token -> token_metadata -> image for every coin launched in the scan window.

Triggers keep it in step with token_metadata.image_content_key and image.phash on every
write path. The lookup still joins token_metadata and image for the coins it finds and
re-checks the hash there, so the copy only narrows which coins are looked at. Existing rows
are backfilled in batches after the triggers exist; the indexes are built concurrently so
the workers keep writing meanwhile.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

BACKFILL_BATCH = 20000


def upgrade() -> None:
    op.execute("alter table token add column if not exists logo_phash bigint")
    op.execute(
        """create or replace function token_logo_phash_from_metadata() returns trigger
           language plpgsql as $$
           begin
             update token t
                set logo_phash = (select i.phash from image i
                                   where i.content_key = new.image_content_key)
              where t.mint = new.mint
                and t.logo_phash is distinct from (select i.phash from image i
                                                    where i.content_key = new.image_content_key);
             return null;
           end $$"""
    )
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
    # a rerun after a failed backfill finds the triggers already there
    for trigger, table in (
        ("token_metadata_logo_phash_ins", "token_metadata"),
        ("token_metadata_logo_phash_upd", "token_metadata"),
        ("image_logo_phash_ins", "image"),
        ("image_logo_phash_upd", "image"),
    ):
        op.execute(f"drop trigger if exists {trigger} on {table}")
    op.execute(
        """create trigger token_metadata_logo_phash_ins after insert on token_metadata
           for each row when (new.image_content_key is not null)
           execute function token_logo_phash_from_metadata()"""
    )
    op.execute(
        """create trigger token_metadata_logo_phash_upd
           after update of image_content_key on token_metadata
           for each row when (old.image_content_key is distinct from new.image_content_key)
           execute function token_logo_phash_from_metadata()"""
    )
    op.execute(
        """create trigger image_logo_phash_ins after insert on image
           for each row when (new.phash is not null)
           execute function token_logo_phash_from_image()"""
    )
    op.execute(
        """create trigger image_logo_phash_upd after update of phash on image
           for each row when (old.phash is distinct from new.phash)
           execute function token_logo_phash_from_image()"""
    )
    with op.get_context().autocommit_block():
        # the image trigger finds a logo's coins by content key
        op.execute("drop index concurrently if exists token_metadata_image_idx")
        op.execute(
            "create index concurrently token_metadata_image_idx"
            " on token_metadata (image_content_key)"
        )
        # backfill in short batches (each commits on its own): no long row locks on token
        bind = op.get_bind()
        last = ""
        while True:
            last = bind.execute(
                sa.text(
                    """with b as (select mint from token where mint > :last
                                   order by mint limit :n),
                            u as (update token t set logo_phash = i.phash
                                    from b, token_metadata tm
                                    join image i on i.content_key = tm.image_content_key
                                   where t.mint = b.mint and tm.mint = b.mint
                                     and i.phash is not null
                                     and t.logo_phash is distinct from i.phash)
                       select max(mint) from b"""
                ),
                {"last": last, "n": BACKFILL_BATCH},
            ).scalar()
            if last is None:
                break
        op.execute("drop index concurrently if exists token_logo_idx")
        op.execute(
            """create index concurrently token_logo_idx on token (created_at)
               include (mint, logo_phash) where logo_phash is not null"""
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("drop index concurrently if exists token_logo_idx")
        op.execute("drop index concurrently if exists token_metadata_image_idx")
    op.execute("drop trigger if exists image_logo_phash_upd on image")
    op.execute("drop trigger if exists image_logo_phash_ins on image")
    op.execute("drop trigger if exists token_metadata_logo_phash_upd on token_metadata")
    op.execute("drop trigger if exists token_metadata_logo_phash_ins on token_metadata")
    op.execute("drop function if exists token_logo_phash_from_image()")
    op.execute("drop function if exists token_logo_phash_from_metadata()")
    op.execute("alter table token drop column if exists logo_phash")
