"""Worker 在线状态（4.1 P9 多 Worker）。

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("host", sa.String(200), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduler", sa.Boolean(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("task_kind", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("owner", name=op.f("pk_worker_heartbeats")),
    )
    op.create_index(op.f("ix_worker_heartbeats_last_seen"), "worker_heartbeats", ["last_seen"])


def downgrade() -> None:
    op.drop_index(op.f("ix_worker_heartbeats_last_seen"), table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
