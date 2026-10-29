"""采集状态表；管理端点增加证书指纹与 SSH 主机密钥固定。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-16 16:29:35.374421

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('collection_status',
    sa.Column('endpoint_id', sa.Uuid(), nullable=False),
    sa.Column('asset_id', sa.Uuid(), nullable=False),
    sa.Column('protocol', sa.String(length=32), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('error_kind', sa.String(length=32), nullable=False),
    sa.Column('message', sa.String(length=1000), nullable=False),
    sa.Column('capabilities', sa.JSON(), nullable=False),
    sa.Column('facts', sa.JSON(), nullable=False),
    sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], name=op.f('fk_collection_status_asset_id_assets'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['endpoint_id'], ['management_endpoints.id'], name=op.f('fk_collection_status_endpoint_id_management_endpoints'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('endpoint_id', name=op.f('pk_collection_status'))
    )
    with op.batch_alter_table('collection_status', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_collection_status_asset_id'), ['asset_id'], unique=False)

    with op.batch_alter_table('management_endpoints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tls_fingerprint', sa.String(length=128), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('ssh_host_key', sa.String(length=512), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('trust_pinned_at', sa.DateTime(timezone=True), nullable=True))



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('management_endpoints', schema=None) as batch_op:
        batch_op.drop_column('trust_pinned_at')
        batch_op.drop_column('ssh_host_key')
        batch_op.drop_column('tls_fingerprint')

    with op.batch_alter_table('collection_status', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_collection_status_asset_id'))

    op.drop_table('collection_status')
