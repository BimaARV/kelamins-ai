"""add memories table (Jarvis persistent memory)

Revision ID: b2c3d4e5f6a7
Revises: a3b9c4d5e6f7
Create Date: 2026-09-16 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a3b9c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('memories',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('meta', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_memories_created_at', 'memories', ['created_at'], unique=False)
    op.create_index('ix_memories_kind', 'memories', ['kind'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_memories_kind', table_name='memories')
    op.drop_index('ix_memories_created_at', table_name='memories')
    op.drop_table('memories')