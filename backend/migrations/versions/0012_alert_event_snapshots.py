"""告警事件保存触发时的指标与阈值快照，不以修改后的规则解释历史。"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alert_events", sa.Column("threshold", sa.Float(), nullable=True))
    op.add_column("alert_events", sa.Column("unit", sa.String(32), nullable=True))
    op.add_column("alert_events", sa.Column("metric_key", sa.String(200), nullable=True))
    op.add_column("alert_events", sa.Column("asset_name", sa.String(200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("alert_events") as batch:
        batch.drop_column("asset_name")
        batch.drop_column("metric_key")
        batch.drop_column("unit")
        batch.drop_column("threshold")
