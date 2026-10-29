"""机柜、型号库与物理定位（4.2 P14）：设备型号表、资产的型号与安装面、交换机 MAC / ARP 观察。

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_types",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manufacturer", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(200), nullable=False, server_default=""),
        sa.Column("part_number", sa.String(100), nullable=False, server_default=""),
        sa.Column("u_height", sa.Float(), nullable=False, server_default="1"),
        sa.Column("is_full_depth", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("interfaces", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("power_ports", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_types")),
        sa.UniqueConstraint("manufacturer", "model", name=op.f("uq_device_types_manufacturer_model")),
    )
    with op.batch_alter_table("assets") as batch:
        batch.add_column(sa.Column("device_type_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("rack_face", sa.String(8), nullable=False, server_default="front"))
        batch.create_foreign_key(op.f("fk_assets_device_type_id_device_types"), "device_types", ["device_type_id"], ["id"], ondelete="SET NULL")
        batch.create_index(op.f("ix_assets_device_type_id"), ["device_type_id"])
    op.create_table(
        "mac_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("switch_asset_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("mac", sa.String(12), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("port", sa.String(200), nullable=False, server_default=""),
        sa.Column("vlan", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["switch_asset_id"], ["assets.id"], ondelete="CASCADE", name=op.f("fk_mac_observations_switch_asset_id_assets")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mac_observations")),
        sa.UniqueConstraint("switch_asset_id", "source", "mac", "ip", "port", name=op.f("uq_mac_observations_switch_asset_id_source_mac_ip_port")),
    )
    op.create_index(op.f("ix_mac_observations_mac"), "mac_observations", ["mac"])
    op.create_index(op.f("ix_mac_observations_ip"), "mac_observations", ["ip"])
    op.create_index(op.f("ix_mac_observations_switch_asset_id"), "mac_observations", ["switch_asset_id"])


def downgrade() -> None:
    op.drop_table("mac_observations")
    with op.batch_alter_table("assets") as batch:
        batch.drop_index(op.f("ix_assets_device_type_id"))
        batch.drop_constraint(op.f("fk_assets_device_type_id_device_types"), type_="foreignkey")
        batch.drop_column("rack_face")
        batch.drop_column("device_type_id")
    op.drop_table("device_types")
