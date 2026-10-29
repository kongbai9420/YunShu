"""虚拟化端点范围、原生观察与显式确认映射。

Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "virtualization_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("target", sa.String(512), nullable=False),
        sa.Column("native_uuid", sa.String(200), nullable=False),
        sa.Column("reported_uuid", sa.String(200), nullable=False),
        sa.Column("identity_conflict", sa.Boolean(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["endpoint_id"], ["management_endpoints.id"], ondelete="CASCADE", name=op.f("fk_virtualization_sources_endpoint_id_management_endpoints")),
        sa.UniqueConstraint("endpoint_id", name=op.f("uq_virtualization_sources_endpoint_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_virtualization_sources")),
    )
    op.create_table(
        "virtualization_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("host_external_id", sa.String(200), nullable=False),
        sa.Column("power", sa.String(16), nullable=False),
        sa.Column("connection", sa.String(32), nullable=False),
        sa.Column("present", sa.Boolean(), nullable=False),
        sa.Column("ignored", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("confirmed_revision", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=True),
        sa.Column("confirmed_host_asset_id", sa.Uuid(), nullable=True),
        sa.Column("owned_relation_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["virtualization_sources.id"], ondelete="CASCADE", name=op.f("fk_virtualization_observations_source_id_virtualization_sources")),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="SET NULL", name=op.f("fk_virtualization_observations_asset_id_assets")),
        sa.ForeignKeyConstraint(["confirmed_host_asset_id"], ["assets.id"], ondelete="SET NULL", name=op.f("fk_virtualization_observations_confirmed_host_asset_id_assets")),
        sa.ForeignKeyConstraint(["owned_relation_id"], ["asset_relations.id"], ondelete="SET NULL", name=op.f("fk_virtualization_observations_owned_relation_id_asset_relations")),
        sa.UniqueConstraint("source_id", "kind", "external_id", name=op.f("uq_virtualization_observations_source_id_kind_external_id")),
        sa.UniqueConstraint("source_id", "asset_id", name=op.f("uq_virtualization_observations_source_id_asset_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_virtualization_observations")),
    )
    op.create_index(op.f("ix_virtualization_observations_source_id"), "virtualization_observations", ["source_id"])
    op.create_index(op.f("ix_virtualization_observations_asset_id"), "virtualization_observations", ["asset_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_virtualization_observations_asset_id"), table_name="virtualization_observations")
    op.drop_index(op.f("ix_virtualization_observations_source_id"), table_name="virtualization_observations")
    op.drop_table("virtualization_observations")
    op.drop_table("virtualization_sources")
