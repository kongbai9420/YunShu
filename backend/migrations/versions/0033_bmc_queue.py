"""每台 BMC 排队与访问频率上限（4.4 P20b）：跨进程租约与访问记录。

Revision ID: 0033
Revises: 0032
"""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bmc_leases",
        sa.Column("key", sa.String(300), nullable=False),
        sa.Column("holder", sa.String(64), nullable=False, server_default=""),
        sa.Column("kind", sa.String(16), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_bmc_leases")),
    )
    op.create_table(
        "bmc_accesses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(300), nullable=False),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bmc_accesses")),
    )
    op.create_index(op.f("ix_bmc_accesses_key"), "bmc_accesses", ["key"])
    op.create_index(op.f("ix_bmc_accesses_at"), "bmc_accesses", ["at"])


def downgrade() -> None:
    op.drop_table("bmc_accesses")
    op.drop_table("bmc_leases")
