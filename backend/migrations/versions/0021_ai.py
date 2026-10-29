"""AI 接入：模型接口、全局设置、调用用量（4.3 P18a）。

Revision ID: 0021
Revises: 0020
"""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_providers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("preset_id", sa.String(64), nullable=False, server_default="custom"),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("api_version", sa.String(32), nullable=False, server_default=""),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("quirks", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("check_result", sa.JSON(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["credential_id"], ["credentials.id"], ondelete="SET NULL", name=op.f("fk_ai_providers_credential_id_credentials")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_ai_providers_name")),
    )
    op.create_table(
        "ai_settings",
        sa.Column("key", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("privacy", sa.String(16), nullable=False, server_default="redact"),
        sa.Column("default_provider_id", sa.Uuid(), nullable=True),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("daily_call_limit", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("daily_token_limit", sa.Integer(), nullable=False, server_default="500000"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("proxy_url", sa.String(500), nullable=False, server_default=""),
        sa.Column("log_content", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("changed_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["default_provider_id"], ["ai_providers.id"], ondelete="SET NULL", name=op.f("fk_ai_settings_default_provider_id_ai_providers")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_ai_settings")),
    )
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("feature", sa.String(32), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=True),
        sa.Column("model", sa.String(200), nullable=False, server_default=""),
        sa.Column("mode", sa.String(16), nullable=False, server_default=""),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error_kind", sa.String(32), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["provider_id"], ["ai_providers.id"], ondelete="SET NULL", name=op.f("fk_ai_usage_provider_id_ai_providers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_usage")),
    )
    op.create_index(op.f("ix_ai_usage_at"), "ai_usage", ["at"])
    op.create_index(op.f("ix_ai_usage_provider_id"), "ai_usage", ["provider_id"])


def downgrade() -> None:
    op.drop_table("ai_usage")
    op.drop_table("ai_settings")
    op.drop_table("ai_providers")
