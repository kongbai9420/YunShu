"""总览仪表盘布局（带修订号）。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16 19:48:53.628024

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('dashboards',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=100), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_dashboards')),
    sa.UniqueConstraint('key', name=op.f('uq_dashboards_key'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('dashboards')
