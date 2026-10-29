"""按状态临时提高采集频率（4.4 P20d）。

Revision ID: 0034
Revises: 0033
"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_boosts",
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], name=op.f("fk_collection_boosts_asset_id_assets"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("asset_id", name=op.f("pk_collection_boosts")),
    )
    op.create_index(op.f("ix_collection_boosts_until"), "collection_boosts", ["until"])


def downgrade() -> None:
    op.drop_table("collection_boosts")
