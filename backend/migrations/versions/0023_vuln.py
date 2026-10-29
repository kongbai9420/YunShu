"""漏洞情报：漏洞记录、来源发布记录、受影响产品、数据源、订阅、设置（4.3 P18c）。

Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vuln_records",
        sa.Column("id", sa.String(80), nullable=False),
        sa.Column("title", sa.String(500), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("severity", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("cvss", sa.Float(), nullable=True),
        sa.Column("cvss_vector", sa.String(200), nullable=False, server_default=""),
        sa.Column("cvss_source", sa.String(40), nullable=False, server_default=""),
        sa.Column("published", sa.DateTime(timezone=True), nullable=True),
        sa.Column("modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kev", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("kev_added", sa.String(10), nullable=False, server_default=""),
        sa.Column("kev_ransomware", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("epss", sa.Float(), nullable=True),
        sa.Column("epss_percentile", sa.Float(), nullable=True),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("references", sa.JSON(), nullable=False),
        sa.Column("cwe", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vuln_records")),
    )
    for col in ("severity", "published", "kev", "first_seen_at"):
        op.create_index(op.f(f"ix_vuln_records_{col}"), "vuln_records", [col])
    op.create_table(
        "vuln_sightings",
        sa.Column("record_id", sa.String(80), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("native_id", sa.String(120), nullable=False, server_default=""),
        sa.Column("title", sa.String(500), nullable=False, server_default=""),
        sa.Column("url", sa.String(1000), nullable=False, server_default=""),
        sa.Column("severity", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("published", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["record_id"], ["vuln_records.id"], ondelete="CASCADE", name=op.f("fk_vuln_sightings_record_id_vuln_records")),
        sa.PrimaryKeyConstraint("record_id", "source", name=op.f("pk_vuln_sightings")),
    )
    op.create_index(op.f("ix_vuln_sightings_seen_at"), "vuln_sightings", ["seen_at"])
    op.create_table(
        "vuln_affects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("ecosystem", sa.String(64), nullable=False, server_default=""),
        sa.Column("product", sa.String(200), nullable=False),
        sa.Column("ranges", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default=""),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["record_id"], ["vuln_records.id"], ondelete="CASCADE", name=op.f("fk_vuln_affects_record_id_vuln_records")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vuln_affects")),
    )
    for col in ("record_id", "ecosystem", "product"):
        op.create_index(op.f(f"ix_vuln_affects_{col}"), "vuln_affects", [col])
    op.create_table(
        "vuln_sources",
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("interval_minutes", sa.Integer(), nullable=False, server_default="1440"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="never"),
        sa.Column("message", sa.String(1000), nullable=False, server_default=""),
        sa.Column("items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_vuln_sources")),
    )
    op.create_table(
        "vuln_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.String(200), nullable=False),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("min_severity", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("kev_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vuln_subscriptions")),
    )
    op.create_table(
        "vuln_settings",
        sa.Column("key", sa.String(16), nullable=False),
        sa.Column("online_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("nvd_credential_id", sa.Uuid(), nullable=True),
        sa.Column("aggregation_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("webhook_token_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("last_bundle_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_bundle_info", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["nvd_credential_id"], ["credentials.id"], ondelete="SET NULL", name=op.f("fk_vuln_settings_nvd_credential_id_credentials")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_vuln_settings")),
    )


def downgrade() -> None:
    for t in ("vuln_settings", "vuln_subscriptions", "vuln_sources", "vuln_affects", "vuln_sightings", "vuln_records"):
        op.drop_table(t)
