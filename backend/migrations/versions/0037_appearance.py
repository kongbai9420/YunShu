"""外观主题（P23）：账号的外观选择、导入的主题包、玻璃主题壁纸。

Revision ID: 0037
Revises: 0036
"""
from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("appearance", sa.JSON(), nullable=True))
    op.create_table(
        "ui_themes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("base", sa.String(16), nullable=False),
        sa.Column("package", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "ui_wallpapers",
        sa.Column("key", sa.String(16), primary_key=True),
        sa.Column("media_type", sa.String(32), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("updated_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ui_wallpapers")
    op.drop_table("ui_themes")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("appearance")
