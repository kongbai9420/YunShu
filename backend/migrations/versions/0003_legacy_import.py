"""导入批次与旧 ID 映射表；资产增加序列号。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16 13:51:32.517546

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('import_batches',
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_import_batches'))
    )
    with op.batch_alter_table('import_batches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_import_batches_created_at'), ['created_at'], unique=False)

    op.create_table('legacy_mappings',
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('legacy_kind', sa.String(length=32), nullable=False),
    sa.Column('legacy_id', sa.String(length=200), nullable=False),
    sa.Column('target_type', sa.String(length=32), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=True),
    sa.Column('endpoint_id', sa.Uuid(), nullable=True),
    sa.Column('credential_id', sa.Uuid(), nullable=True),
    sa.Column('target_revision', sa.Integer(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('payload_hash', sa.String(length=64), nullable=False),
    sa.Column('batch_id', sa.Uuid(), nullable=True),
    sa.Column('imported_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['import_batches.id'], name=op.f('fk_legacy_mappings_batch_id_import_batches'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_legacy_mappings')),
    sa.UniqueConstraint('source', 'legacy_kind', 'legacy_id', name=op.f('uq_legacy_mappings_source_legacy_kind_legacy_id'))
    )
    with op.batch_alter_table('assets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('serial_no', sa.String(length=100), nullable=False, server_default=''))



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('assets', schema=None) as batch_op:
        batch_op.drop_column('serial_no')

    op.drop_table('legacy_mappings')
    with op.batch_alter_table('import_batches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_import_batches_created_at'))

    op.drop_table('import_batches')
