"""服务器告警推送（4.4 P20c）：平台在 BMC 上配置的 Redfish 事件订阅与 SNMP Trap 目标。

Revision ID: 0031
Revises: 0030
"""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bmc_event_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("destination", sa.String(500), nullable=False, server_default=""),
        sa.Column("remote_id", sa.String(300), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("events_received", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], name=op.f("fk_bmc_event_subscriptions_asset_id_assets"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bmc_event_subscriptions")),
        sa.UniqueConstraint("asset_id", "kind", name=op.f("uq_bmc_event_subscriptions_asset_id_kind")),
    )
    op.create_index(op.f("ix_bmc_event_subscriptions_asset_id"), "bmc_event_subscriptions", ["asset_id"])
    op.create_index(op.f("ix_bmc_event_subscriptions_token_hash"), "bmc_event_subscriptions", ["token_hash"])


def downgrade() -> None:
    op.drop_table("bmc_event_subscriptions")
