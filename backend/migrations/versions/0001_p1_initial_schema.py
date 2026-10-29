"""P1 首版表结构：资产、管理端点、场所与机柜、资产关系、凭据、用户、审计。

Revision ID: 0001
Revises: 
Create Date: 2026-10-16 13:34:20.100300

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('credentials',
    sa.Column('alias', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('username', sa.String(length=200), nullable=False),
    sa.Column('secret_ciphertext', sa.LargeBinary(), nullable=False),
    sa.Column('secret_nonce', sa.LargeBinary(), nullable=False),
    sa.Column('key_id', sa.String(length=64), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('legacy_source', sa.String(length=64), nullable=True),
    sa.Column('legacy_id', sa.String(length=200), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credentials')),
    sa.UniqueConstraint('alias', name=op.f('uq_credentials_alias')),
    sa.UniqueConstraint('legacy_source', 'legacy_id', name=op.f('uq_credentials_legacy_source_legacy_id'))
    )
    op.create_table('sites',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['sites.id'], name=op.f('fk_sites_parent_id_sites'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sites'))
    )
    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sites_parent_id'), ['parent_id'], unique=False)

    op.create_table('users',
    sa.Column('username', sa.String(length=100), nullable=False),
    sa.Column('password_hash', sa.String(length=512), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('legacy_source', sa.String(length=64), nullable=True),
    sa.Column('legacy_id', sa.String(length=200), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('legacy_source', 'legacy_id', name=op.f('uq_users_legacy_source_legacy_id')),
    sa.UniqueConstraint('username', name=op.f('uq_users_username'))
    )
    op.create_table('audit_events',
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=True),
    sa.Column('actor_name', sa.String(length=100), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=32), nullable=False),
    sa.Column('target_id', sa.String(length=64), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name=op.f('fk_audit_events_actor_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_events_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_actor_id'), ['actor_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_occurred_at'), ['occurred_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_target_id'), ['target_id'], unique=False)

    op.create_table('racks',
    sa.Column('site_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('height_u', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_racks_site_id_sites'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_racks')),
    sa.UniqueConstraint('site_id', 'name', name=op.f('uq_racks_site_id_name'))
    )
    with op.batch_alter_table('racks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_racks_site_id'), ['site_id'], unique=False)

    op.create_table('assets',
    sa.Column('asset_no', sa.String(length=100), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('form', sa.String(length=32), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('vendor', sa.String(length=100), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('lifecycle', sa.String(length=32), nullable=False),
    sa.Column('site_id', sa.Uuid(), nullable=True),
    sa.Column('rack_id', sa.Uuid(), nullable=True),
    sa.Column('rack_unit', sa.Integer(), nullable=True),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('legacy_source', sa.String(length=64), nullable=True),
    sa.Column('legacy_id', sa.String(length=200), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['rack_id'], ['racks.id'], name=op.f('fk_assets_rack_id_racks'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_assets_site_id_sites'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assets')),
    sa.UniqueConstraint('asset_no', name=op.f('uq_assets_asset_no')),
    sa.UniqueConstraint('legacy_source', 'legacy_id', name=op.f('uq_assets_legacy_source_legacy_id'))
    )
    with op.batch_alter_table('assets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assets_name'), ['name'], unique=False)
        batch_op.create_index(batch_op.f('ix_assets_rack_id'), ['rack_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_assets_site_id'), ['site_id'], unique=False)

    op.create_table('asset_relations',
    sa.Column('source_asset_id', sa.Uuid(), nullable=False),
    sa.Column('target_asset_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('origin', sa.String(length=32), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['source_asset_id'], ['assets.id'], name=op.f('fk_asset_relations_source_asset_id_assets'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_asset_id'], ['assets.id'], name=op.f('fk_asset_relations_target_asset_id_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_asset_relations')),
    sa.UniqueConstraint('source_asset_id', 'target_asset_id', 'kind', name=op.f('uq_asset_relations_source_asset_id_target_asset_id_kind'))
    )
    with op.batch_alter_table('asset_relations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_asset_relations_source_asset_id'), ['source_asset_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_asset_relations_target_asset_id'), ['target_asset_id'], unique=False)

    op.create_table('management_endpoints',
    sa.Column('asset_id', sa.Uuid(), nullable=False),
    sa.Column('address', sa.String(length=255), nullable=False),
    sa.Column('port', sa.Integer(), nullable=True),
    sa.Column('protocol', sa.String(length=32), nullable=False),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('credential_id', sa.Uuid(), nullable=True),
    sa.Column('tls_verify', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], name=op.f('fk_management_endpoints_asset_id_assets'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['credential_id'], ['credentials.id'], name=op.f('fk_management_endpoints_credential_id_credentials'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_management_endpoints'))
    )
    with op.batch_alter_table('management_endpoints', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_management_endpoints_address'), ['address'], unique=False)
        batch_op.create_index(batch_op.f('ix_management_endpoints_asset_id'), ['asset_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_management_endpoints_credential_id'), ['credential_id'], unique=False)



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('management_endpoints', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_management_endpoints_credential_id'))
        batch_op.drop_index(batch_op.f('ix_management_endpoints_asset_id'))
        batch_op.drop_index(batch_op.f('ix_management_endpoints_address'))

    op.drop_table('management_endpoints')
    with op.batch_alter_table('asset_relations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_asset_relations_target_asset_id'))
        batch_op.drop_index(batch_op.f('ix_asset_relations_source_asset_id'))

    op.drop_table('asset_relations')
    with op.batch_alter_table('assets', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_assets_site_id'))
        batch_op.drop_index(batch_op.f('ix_assets_rack_id'))
        batch_op.drop_index(batch_op.f('ix_assets_name'))

    op.drop_table('assets')
    with op.batch_alter_table('racks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_racks_site_id'))

    op.drop_table('racks')
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_events_target_id'))
        batch_op.drop_index(batch_op.f('ix_audit_events_occurred_at'))
        batch_op.drop_index(batch_op.f('ix_audit_events_actor_id'))
        batch_op.drop_index(batch_op.f('ix_audit_events_action'))

    op.drop_table('audit_events')
    op.drop_table('users')
    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sites_parent_id'))

    op.drop_table('sites')
    op.drop_table('credentials')
