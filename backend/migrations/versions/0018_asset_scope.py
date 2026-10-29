"""按资产范围授权（4.1 P11）：现有账号全部设为全部可见，行为与升级前相同。

Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("scope_all", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table(
        "user_scope_sites",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name=op.f("fk_user_scope_sites_user_id_users")),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE", name=op.f("fk_user_scope_sites_site_id_sites")),
        sa.PrimaryKeyConstraint("user_id", "site_id", name=op.f("pk_user_scope_sites")),
    )
    op.create_table(
        "user_scope_assets",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name=op.f("fk_user_scope_assets_user_id_users")),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE", name=op.f("fk_user_scope_assets_asset_id_assets")),
        sa.PrimaryKeyConstraint("user_id", "asset_id", name=op.f("pk_user_scope_assets")),
    )


def downgrade() -> None:
    op.drop_table("user_scope_assets")
    op.drop_table("user_scope_sites")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("scope_all")
