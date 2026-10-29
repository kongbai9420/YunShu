"""服务器告警（4.4 P20c）：BMC 系统事件日志等来源的逐条记录与处理状态。

Revision ID: 0030
Revises: 0029
"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("record_key", sa.String(160), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("category", sa.String(16), nullable=False, server_default="other"),
        sa.Column("message", sa.String(1000), nullable=False, server_default=""),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("handled_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(500), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], name=op.f("fk_server_events_asset_id_assets"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_server_events")),
        sa.UniqueConstraint("asset_id", "source", "record_key", name=op.f("uq_server_events_asset_id_source_record_key")),
    )
    op.create_index(op.f("ix_server_events_asset_id"), "server_events", ["asset_id"])
    op.create_index(op.f("ix_server_events_occurred_at"), "server_events", ["occurred_at"])
    op.create_index(op.f("ix_server_events_severity"), "server_events", ["severity"])
    op.create_index(op.f("ix_server_events_state"), "server_events", ["state"])


def downgrade() -> None:
    op.drop_table("server_events")
