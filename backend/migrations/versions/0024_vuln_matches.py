"""漏洞比对结果与处置（4.3 P18d）。

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vuln_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.String(80), nullable=False),
        sa.Column("product", sa.String(260), nullable=False),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("installed_version", sa.String(200), nullable=False, server_default=""),
        sa.Column("fixed_version", sa.String(200), nullable=False, server_default=""),
        sa.Column("confidence", sa.String(16), nullable=False, server_default="affected"),
        sa.Column("note", sa.String(500), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("review_at", sa.Date(), nullable=True),
        sa.Column("status_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE", name=op.f("fk_vuln_matches_asset_id_assets")),
        sa.ForeignKeyConstraint(["record_id"], ["vuln_records.id"], ondelete="CASCADE", name=op.f("fk_vuln_matches_record_id_vuln_records")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vuln_matches")),
        sa.UniqueConstraint("asset_id", "record_id", "product", name=op.f("uq_vuln_matches_asset_id_record_id_product")),
    )
    for col in ("asset_id", "record_id", "active", "status"):
        op.create_index(op.f(f"ix_vuln_matches_{col}"), "vuln_matches", [col])


def downgrade() -> None:
    op.drop_table("vuln_matches")
