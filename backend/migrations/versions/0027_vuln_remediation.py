"""漏洞的修复与缓解原文、KEV 处置期限（4.3 P18 补充）。

Revision ID: 0027
Revises: 0026
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vuln_records") as batch:
        batch.add_column(sa.Column("remediation", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("kev_due", sa.String(10), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("vuln_records") as batch:
        batch.drop_column("kev_due")
        batch.drop_column("remediation")
