"""模板导入逐行记录；导入批次增加模板类型、状态时间；资产增加采集模板名称。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16 14:10:44.962760

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('import_items',
    sa.Column('batch_id', sa.Uuid(), nullable=False),
    sa.Column('row_no', sa.Integer(), nullable=False),
    sa.Column('asset_no', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('messages', sa.JSON(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('asset_id', sa.Uuid(), nullable=True),
    sa.Column('base_revision', sa.Integer(), nullable=True),
    sa.Column('after_revision', sa.Integer(), nullable=True),
    sa.Column('before', sa.JSON(), nullable=False),
    sa.Column('created_refs', sa.JSON(), nullable=False),
    sa.Column('undo_result', sa.String(length=16), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['import_batches.id'], name=op.f('fk_import_items_batch_id_import_batches'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_import_items'))
    )
    with op.batch_alter_table('import_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_import_items_batch_id'), ['batch_id'], unique=False)

    with op.batch_alter_table('assets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('collection_profile', sa.String(length=100), nullable=False, server_default=''))

    with op.batch_alter_table('import_batches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('template_kind', sa.String(length=32), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('undone_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f('ix_import_batches_status'), ['status'], unique=False)



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('import_batches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_import_batches_status'))
        batch_op.drop_column('undone_at')
        batch_op.drop_column('applied_at')
        batch_op.drop_column('template_kind')

    with op.batch_alter_table('assets', schema=None) as batch_op:
        batch_op.drop_column('collection_profile')

    with op.batch_alter_table('import_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_import_items_batch_id'))

    op.drop_table('import_items')
