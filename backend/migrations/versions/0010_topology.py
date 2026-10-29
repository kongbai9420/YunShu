"""拓扑：网络链路（候选 / 已确认 / 人工）与画布视图（布局带修订号），预置家用与机房两个视图。

Revision ID: 0010
Revises: 0009
"""
import uuid
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "topology_views",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("layout", sa.JSON(), nullable=False),
        sa.Column("hidden", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], name=op.f("fk_topology_views_site_id_sites"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topology_views")),
        sa.UniqueConstraint("name", name=op.f("uq_topology_views_name")),
    )
    with op.batch_alter_table("topology_views") as batch_op:
        batch_op.create_index(batch_op.f("ix_topology_views_site_id"), ["site_id"], unique=False)

    op.create_table(
        "network_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_asset_id", sa.Uuid(), nullable=False),
        sa.Column("target_asset_id", sa.Uuid(), nullable=False),
        sa.Column("source_port", sa.String(200), nullable=False),
        sa.Column("target_port", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(255), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.String(500), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_asset_id"], ["assets.id"], name=op.f("fk_network_links_source_asset_id_assets"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_asset_id"], ["assets.id"], name=op.f("fk_network_links_target_asset_id_assets"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_network_links")),
    )
    with op.batch_alter_table("network_links") as batch_op:
        batch_op.create_index(batch_op.f("ix_network_links_fingerprint"), ["fingerprint"], unique=False)
        batch_op.create_index(batch_op.f("ix_network_links_source_asset_id"), ["source_asset_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_network_links_state"), ["state"], unique=False)
        batch_op.create_index(batch_op.f("ix_network_links_target_asset_id"), ["target_asset_id"], unique=False)

    # 预置两个空视图（落地方案 P3“机房与家用视图”）；布局为空，页面首次打开时自动布局
    views = sa.table(
        "topology_views",
        sa.column("id", sa.Uuid()), sa.column("name", sa.String()), sa.column("kind", sa.String()),
        sa.column("layout", sa.JSON()), sa.column("hidden", sa.JSON()), sa.column("revision", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)), sa.column("updated_by", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    op.bulk_insert(views, [
        {"id": uuid.uuid4(), "name": name, "kind": kind, "layout": {}, "hidden": [], "revision": 1,
         "created_at": now, "updated_by": "", "updated_at": now}
        for name, kind in (("家庭网络", "home"), ("机房", "datacenter"))
    ])


def downgrade() -> None:
    op.drop_table("network_links")
    op.drop_table("topology_views")
