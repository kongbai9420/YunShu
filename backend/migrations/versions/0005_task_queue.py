"""持久任务队列与单实例调度锁（P2）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16 15:39:15.628197

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('scheduler_locks',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('owner', sa.String(length=100), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_scheduler_locks'))
    )
    op.create_table('tasks',
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('risk', sa.String(length=16), nullable=False),
    sa.Column('asset_id', sa.Uuid(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('lease_owner', sa.String(length=100), nullable=False),
    sa.Column('lease_token', sa.String(length=64), nullable=False),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('idempotency_key', sa.String(length=200), nullable=True),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], name=op.f('fk_tasks_asset_id_assets'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tasks')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_tasks_idempotency_key'))
    )
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tasks_asset_id'), ['asset_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_available_at'), ['available_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_lease_expires_at'), ['lease_expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_status'), ['status'], unique=False)



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tasks_status'))
        batch_op.drop_index(batch_op.f('ix_tasks_lease_expires_at'))
        batch_op.drop_index(batch_op.f('ix_tasks_kind'))
        batch_op.drop_index(batch_op.f('ix_tasks_available_at'))
        batch_op.drop_index(batch_op.f('ix_tasks_asset_id'))

    op.drop_table('tasks')
    op.drop_table('scheduler_locks')
