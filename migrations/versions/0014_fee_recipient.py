"""fee_recipient: what a creator-fee shareholder address is (a wallet, a GitHub-linked
SocialFeePda, a charity DonationFeePda), so a recipient shared by many coins is read from
the chain once (rules 0.19.0).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-08
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists fee_recipient (
          address text primary key,
          kind text not null,
          platform text,
          user_id text,
          github_login text,
          charity_config_id text,
          lifetime_lamports bigint,
          quote_mint text,
          resolved_at timestamptz not null default now()
        )
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists fee_recipient")
