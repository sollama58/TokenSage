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
    # Every statement commits on its own: the ACCESS EXCLUSIVE lock of ADD COLUMN on token
    # is released before the triggers lock token_metadata and image, so the migration cannot
    # deadlock with the old worker's metadata writes (they lock token_metadata, then check
    # the FK on token). lock_timeout keeps token readers from queueing behind a DDL that
    # waits; a timed-out run fails the deploy and a rerun picks up where it stopped.
    with op.get_context().autocommit_block():
        op.execute("set lock_timeout = '10s'")
        op.execute("alter table token add column if not exists logo_phash bigint")
        # `for share` on the image row serialises this with a concurrent phash write for
        # the same logo: either this read waits for that commit and sees the new hash, or
        # that write waits for this insert and its trigger then finds the new coin.
        op.execute(
            """create or replace function token_logo_phash_from_metadata() returns trigger
               language plpgsql as $$
               declare h bigint;
               begin
                 select i.phash into h from image i
                  where i.content_key = new.image_content_key for share;
                 update token t set logo_phash = h
                  where t.mint = new.mint and t.logo_phash is distinct from h;
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
        op.execute("set lock_timeout = 0")
        # the image trigger finds a logo's coins by content key
        op.execute("drop index concurrently if exists token_metadata_image_idx")
        op.execute(
            "create index concurrently token_metadata_image_idx"
            " on token_metadata (image_content_key)"
        )
        # Backfill in short batches (each commits on its own): no long row locks on token.
        # The source rows are read `for share`, so a logo change committing meanwhile is
        # either waited for (and then the changed row no longer joins: its trigger already
        # wrote the new hash) or waits for this batch and then overwrites it.
        bind = op.get_bind()
        last = ""
        while True:
            last = bind.execute(
                sa.text(
                    """with b as (select mint from token where mint > :last
                                   order by mint limit :n),
                            src as (select tm.mint, i.phash
                                      from b
                                      join token_metadata tm on tm.mint = b.mint
                                      join image i on i.content_key = tm.image_content_key
                                     where i.phash is not null
                                       for share of tm, i),
                            u as (update token t set logo_phash = src.phash
                                    from src
                                   where t.mint = src.mint
                                     and t.logo_phash is distinct from src.phash)
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
