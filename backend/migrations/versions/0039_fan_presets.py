"""情景方案与按风扇设定转速。

Revision ID: 0039
Revises: 0038
"""
from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("control_targets") as batch:
        batch.add_column(sa.Column("applied_speeds", sa.JSON(), nullable=True))
    with op.batch_alter_table("thermal_policies") as batch:
        batch.add_column(sa.Column("preset_key", sa.String(32), nullable=False, server_default=""))
    with op.batch_alter_table("thermal_command_profiles") as batch:
        batch.add_column(sa.Column("fan_commands", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("thermal_command_profiles") as batch:
        batch.drop_column("fan_commands")
    with op.batch_alter_table("thermal_policies") as batch:
        batch.drop_column("preset_key")
    with op.batch_alter_table("control_targets") as batch:
        batch.drop_column("applied_speeds")
