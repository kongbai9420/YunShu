"""P5 温控：控制目标记录已下发的温控模式与转速。"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("control_targets") as batch:
        batch.add_column(sa.Column("thermal_mode", sa.String(16), nullable=False, server_default="unknown"))
        batch.add_column(sa.Column("applied_pwm", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("thermal_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("control_targets") as batch:
        batch.drop_column("thermal_updated_at")
        batch.drop_column("applied_pwm")
        batch.drop_column("thermal_mode")
