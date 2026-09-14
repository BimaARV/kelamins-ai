"""add document engine tables

Revision ID: a3b9c4d5e6f7
Revises: d1ad0b68dba5
Create Date: 2026-09-11 06:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = 'a3b9c4d5e6f7'
down_revision: Union[str, None] = 'd1ad0b68dba5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('documents',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('filename', sa.String(length=500), nullable=False),
    sa.Column('original_filename', sa.String(length=500), nullable=False),
    sa.Column('file_path', sa.String(length=1000), nullable=False),
    sa.Column('document_type', sa.Enum('pdf', 'docx', 'txt', 'markdown', 'image', 'other', name='document_type_enum'), nullable=False),
    sa.Column('file_size', sa.Integer(), nullable=False),
    sa.Column('text_content', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=True),
    sa.Column('meta', sa.JSON(), nullable=True),
    sa.Column('processing_status', sa.String(length=20), nullable=False),
    sa.Column('event_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_documents_document_type', 'documents', ['document_type'], unique=False)
    op.create_index('ix_documents_created_at', 'documents', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_documents_created_at', table_name='documents')
    op.drop_index('ix_documents_document_type', table_name='documents')
    op.drop_table('documents')