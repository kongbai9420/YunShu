"""软件清单：已安装软件包、读取结果、端点开关（4.3 P18b）。

Revision ID: 0022
Revises: 0021
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("management_endpoints") as batch:
        batch.add_column(sa.Column("collect_packages", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "asset_packages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("ecosystem", sa.String(64), nullable=False, server_default=""),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.String(200), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("source_version", sa.String(200), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE", name=op.f("fk_asset_packages_asset_id_assets")),
        sa.ForeignKeyConstraint(["endpoint_id"], ["management_endpoints.id"], ondelete="CASCADE", name=op.f("fk_asset_packages_endpoint_id_management_endpoints")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_packages")),
        sa.UniqueConstraint("endpoint_id", "name", name=op.f("uq_asset_packages_endpoint_id_name")),
    )
    op.create_index(op.f("ix_asset_packages_asset_id"), "asset_packages", ["asset_id"])
    op.create_index(op.f("ix_asset_packages_ecosystem"), "asset_packages", ["ecosystem"])
    op.create_index(op.f("ix_asset_packages_source"), "asset_packages", ["source"])
    op.create_table(
        "package_scans",
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("message", sa.String(1000), nullable=False, server_default=""),
        sa.Column("manager", sa.String(16), nullable=False, server_default=""),
        sa.Column("ecosystem", sa.String(64), nullable=False, server_default=""),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE", name=op.f("fk_package_scans_asset_id_assets")),
        sa.ForeignKeyConstraint(["endpoint_id"], ["management_endpoints.id"], ondelete="CASCADE", name=op.f("fk_package_scans_endpoint_id_management_endpoints")),
        sa.PrimaryKeyConstraint("endpoint_id", name=op.f("pk_package_scans")),
    )
    op.create_index(op.f("ix_package_scans_asset_id"), "package_scans", ["asset_id"])


def downgrade() -> None:
    op.drop_table("package_scans")
    op.drop_table("asset_packages")
    with op.batch_alter_table("management_endpoints") as batch:
        batch.drop_column("collect_packages")
