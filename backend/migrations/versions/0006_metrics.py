"""指标：最新值、压缩存放的分钟 / 小时汇总、汇总水位（原始数据按天分表，由代码管理）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16 16:16:29.312365

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('metric_state',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_metric_state'))
    )
    op.create_table('latest_metrics',
    sa.Column('asset_id', sa.Uuid(), nullable=False),
    sa.Column('metric_key', sa.String(length=200), nullable=False),
    sa.Column('value', sa.Double(), nullable=True),
    sa.Column('unit', sa.String(length=32), nullable=False),
    sa.Column('quality', sa.String(length=16), nullable=False),
    sa.Column('estimated', sa.Boolean(), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('interval_seconds', sa.Integer(), nullable=False),
    sa.Column('last_valid_value', sa.Double(), nullable=True),
    sa.Column('last_valid_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], name=op.f('fk_latest_metrics_asset_id_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('asset_id', 'metric_key', name=op.f('pk_latest_metrics'))
    )
    op.create_table('metric_rollups',
    sa.Column('asset_id', sa.Uuid(), nullable=False),
    sa.Column('resolution', sa.Integer(), nullable=False),
    sa.Column('bucket', sa.Integer(), nullable=False),
    sa.Column('data', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], name=op.f('fk_metric_rollups_asset_id_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('asset_id', 'resolution', 'bucket', name=op.f('pk_metric_rollups'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('metric_rollups')
    op.drop_table('latest_metrics')
    op.drop_table('metric_state')
