"""AI 告警解读与巡检摘要（4.3 P18f）。

Revision ID: 0026
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("ref", sa.String(80), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False, server_default=""),
        sa.Column("model", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_notes")),
        sa.UniqueConstraint("kind", "ref", "context_hash", name=op.f("uq_ai_notes_kind_ref_context_hash")),
    )
    op.create_index(op.f("ix_ai_notes_ref"), "ai_notes", ["ref"])


def downgrade() -> None:
    op.drop_table("ai_notes")
