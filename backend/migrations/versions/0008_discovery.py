"""只读网段发现与待确认观察记录。

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("excludes", sa.JSON(), nullable=False),
        sa.Column("addresses", sa.JSON(), nullable=False),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("ports", sa.JSON(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), sa.ForeignKey("credentials.id", ondelete="SET NULL")),
        sa.Column("site_id", sa.Uuid(), sa.ForeignKey("sites.id", ondelete="SET NULL")),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("timeout", sa.Float(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("responded", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(500), nullable=False),
    )
    op.create_index("ix_discovery_runs_state", "discovery_runs", ["state"])
    op.create_table(
        "discovery_observations",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("address", sa.String(255), nullable=False),
        sa.Column("ports", sa.JSON(), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.UniqueConstraint("run_id", "address"),
    )
    op.create_index("ix_discovery_observations_run_id", "discovery_observations", ["run_id"])


def downgrade() -> None:
    op.drop_table("discovery_observations")
    op.drop_table("discovery_runs")
