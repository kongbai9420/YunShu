"""管理员自定义风扇命令。

Revision ID: 0038
Revises: 0037
"""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thermal_command_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("vendor", sa.String(64), nullable=False),
        sa.Column("model_pattern", sa.String(200), nullable=False, server_default=""),
        sa.Column("firmware_min", sa.String(32), nullable=False, server_default=""),
        sa.Column("firmware_max", sa.String(32), nullable=False, server_default=""),
        sa.Column("restore_commands", sa.Text(), nullable=False),
        sa.Column("set_commands", sa.Text(), nullable=False),
        sa.Column("pwm_scale", sa.String(16), nullable=False, server_default="percent"),
        sa.Column("safe_min_pwm", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("safe_max_pwm", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_thermal_command_profiles")),
    )


def downgrade() -> None:
    op.drop_table("thermal_command_profiles")
