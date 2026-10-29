"""登录会话表；用户增加“需要修改密码”标记。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 13:41:27.354742

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('login_sessions',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('csrf_token', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('client_ip', sa.String(length=64), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_login_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_login_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_login_sessions_token_hash'))
    )
    with op.batch_alter_table('login_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_login_sessions_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_login_sessions_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('require_password_change', sa.Boolean(), nullable=False, server_default=sa.false())
        )



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('require_password_change')

    with op.batch_alter_table('login_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_login_sessions_user_id'))
        batch_op.drop_index(batch_op.f('ix_login_sessions_expires_at'))

    op.drop_table('login_sessions')
