"""Add required_channels (forced-sub) and referral verification columns.

Adds:
  * giveaways.required_channels        (forced-subscription channel list)
  * group_giveaways.required_channels  (forced-subscription channel list)
  * referrals.verified                 (referral only counts once verified)

The bot also creates tables via SQLAlchemy ``create_all`` at startup, so this
migration is written defensively: each column is only added when its table
already exists and the column is not present yet. This makes it safe to run on
databases that were bootstrapped either by Alembic or by ``create_all``.

Revision ID: 0001_reqchan_refverified
Revises:
Create Date: 2026-07-06
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0001_reqchan_refverified"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def _has_column(inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "giveaways") and not _has_column(inspector, "giveaways", "required_channels"):
        op.add_column("giveaways", sa.Column("required_channels", sa.Text(), nullable=True))

    if _has_table(inspector, "group_giveaways") and not _has_column(inspector, "group_giveaways", "required_channels"):
        op.add_column("group_giveaways", sa.Column("required_channels", sa.Text(), nullable=True))

    if _has_table(inspector, "referrals") and not _has_column(inspector, "referrals", "verified"):
        # server_default=false backfills existing rows to unverified and is a
        # harmless, portable default (the app always sets the value explicitly).
        op.add_column(
            "referrals",
            sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "referrals") and _has_column(inspector, "referrals", "verified"):
        op.drop_column("referrals", "verified")

    if _has_table(inspector, "group_giveaways") and _has_column(inspector, "group_giveaways", "required_channels"):
        op.drop_column("group_giveaways", "required_channels")

    if _has_table(inspector, "giveaways") and _has_column(inspector, "giveaways", "required_channels"):
        op.drop_column("giveaways", "required_channels")
