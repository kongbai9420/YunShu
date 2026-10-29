"""采集计划双来源核对（4.4 P20e）。

Revision ID: 0035
Revises: 0034
"""
from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("collection_plans") as batch:
        batch.add_column(sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("check", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    with op.batch_alter_table("collection_plans") as batch:
        batch.drop_column("check")
        batch.drop_column("checked_at")
