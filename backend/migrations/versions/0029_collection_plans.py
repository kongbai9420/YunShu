"""采集计划（4.4 P20a）：每台服务器 BMC 探测到的协议能力与每项数据的主来源。

Revision ID: 0029
Revises: 0028
"""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_plans",
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("probed", sa.JSON(), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False, server_default=""),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reprobe", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], name=op.f("fk_collection_plans_asset_id_assets"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("asset_id", name=op.f("pk_collection_plans")),
    )


def downgrade() -> None:
    op.drop_table("collection_plans")
