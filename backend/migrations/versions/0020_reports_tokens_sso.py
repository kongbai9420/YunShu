"""巡检报告、只读 API 令牌与单点登录账号（4.2 P16）。

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inspection_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("asset_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspection_reports")),
    )
    op.create_index(op.f("ix_inspection_reports_created_at"), "inspection_reports", ["created_at"])
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False, server_default="metrics"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name=op.f("fk_api_tokens_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )
    op.create_index(op.f("ix_api_tokens_user_id"), "api_tokens", ["user_id"])
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("auth_source", sa.String(16), nullable=False, server_default="local"))
        batch.add_column(sa.Column("external_subject", sa.String(255), nullable=True))
        batch.create_unique_constraint(op.f("uq_users_external_subject"), ["external_subject"])


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint(op.f("uq_users_external_subject"), type_="unique")
        batch.drop_column("external_subject")
        batch.drop_column("auth_source")
    op.drop_table("api_tokens")
    op.drop_table("inspection_reports")
