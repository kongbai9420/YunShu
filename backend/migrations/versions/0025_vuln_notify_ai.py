"""漏洞通知与 AI 影响分析（4.3 P18e）。

Revision ID: 0025
Revises: 0024
"""
from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("notification_deliveries") as batch:
        batch.add_column(sa.Column("kind", sa.String(16), nullable=False, server_default="alert"))
    with op.batch_alter_table("vuln_settings") as batch:
        batch.add_column(sa.Column("notify_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("notify_channel_ids", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("digest_hour", sa.Integer(), nullable=False, server_default="9"))
        batch.add_column(sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("vuln_matches") as batch:
        batch.add_column(sa.Column("notified_kev", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "vuln_analyses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.String(80), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False, server_default=""),
        sa.Column("model", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["record_id"], ["vuln_records.id"], ondelete="CASCADE", name=op.f("fk_vuln_analyses_record_id_vuln_records")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vuln_analyses")),
        sa.UniqueConstraint("record_id", "context_hash", name=op.f("uq_vuln_analyses_record_id_context_hash")),
    )
    op.create_index(op.f("ix_vuln_analyses_record_id"), "vuln_analyses", ["record_id"])
    op.create_index(op.f("ix_vuln_analyses_created_at"), "vuln_analyses", ["created_at"])


def downgrade() -> None:
    op.drop_table("vuln_analyses")
    with op.batch_alter_table("vuln_matches") as batch:
        batch.drop_column("notified_kev")
    with op.batch_alter_table("vuln_settings") as batch:
        for c in ("last_digest_at", "digest_hour", "notify_channel_ids", "notify_enabled"):
            batch.drop_column(c)
    with op.batch_alter_table("notification_deliveries") as batch:
        batch.drop_column("kind")
