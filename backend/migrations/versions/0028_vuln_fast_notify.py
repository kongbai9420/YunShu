"""漏洞尽快推送（4.3 P18 补充）：订阅命中预警、CVE 官方记录来源、缩短默认拉取间隔与汇总窗口。

只改仍是旧默认值的行；用户自己改过的间隔与汇总窗口保持不变。

Revision ID: 0028
Revises: 0027
"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

# 来源：旧默认间隔 → 新默认间隔（分钟）
INTERVALS = {"nvd": (360, 60), "kev": (360, 60), "chaitin": (60, 10), "oscs": (60, 10), "ti": (60, 10), "threatbook": (60, 10)}


def upgrade() -> None:
    with op.batch_alter_table("vuln_settings") as batch:
        batch.add_column(sa.Column("early_warning", sa.Boolean(), nullable=False, server_default=sa.true()))
    with op.batch_alter_table("vuln_records") as batch:
        batch.add_column(sa.Column("early_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE vuln_settings SET aggregation_minutes = 5 WHERE aggregation_minutes = 30")
    for name, (old, new) in INTERVALS.items():
        op.execute(sa.text("UPDATE vuln_sources SET interval_minutes = :new WHERE name = :name AND interval_minutes = :old")
                   .bindparams(new=new, name=name, old=old))


def downgrade() -> None:
    for name, (old, new) in INTERVALS.items():
        op.execute(sa.text("UPDATE vuln_sources SET interval_minutes = :old WHERE name = :name AND interval_minutes = :new")
                   .bindparams(new=new, name=name, old=old))
    op.execute("UPDATE vuln_settings SET aggregation_minutes = 30 WHERE aggregation_minutes = 5")
    with op.batch_alter_table("vuln_records") as batch:
        batch.drop_column("early_notified_at")
    with op.batch_alter_table("vuln_settings") as batch:
        batch.drop_column("early_warning")
