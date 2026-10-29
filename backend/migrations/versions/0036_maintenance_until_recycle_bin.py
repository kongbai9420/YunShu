"""维护预计结束时间与资产回收站。

Revision ID: 0036
Revises: 0035
"""
from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("assets") as batch:
        batch.add_column(sa.Column("maintenance_until", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("deleted_by", sa.String(100), nullable=False, server_default=""))
        batch.add_column(sa.Column("deleted_lifecycle", sa.String(32), nullable=False, server_default=""))
        batch.create_index("ix_assets_deleted_at", ["deleted_at"])
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("platform_settings")
    with op.batch_alter_table("assets") as batch:
        batch.drop_index("ix_assets_deleted_at")
        batch.drop_column("deleted_lifecycle")
        batch.drop_column("deleted_by")
        batch.drop_column("deleted_at")
        batch.drop_column("maintenance_until")
