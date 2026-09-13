"""add message reports table

Revision ID: a3f8c1d92e77
Revises: 1f3ae67fca84
Create Date: 2026-09-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import diffdx.db.types


# revision identifiers, used by Alembic.
revision: str = 'a3f8c1d92e77'
down_revision: Union[str, None] = '1f3ae67fca84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('message_reports',
    sa.Column('id', diffdx.db.types.GUID(), nullable=False),
    sa.Column('reporter_user_id', diffdx.db.types.GUID(), nullable=False),
    sa.Column('reporter_role', sa.String(length=20), nullable=False),
    sa.Column('reporter_name', sa.String(length=200), nullable=False),
    sa.Column('thread_id', sa.String(length=200), nullable=False),
    sa.Column('reported_patient_user_id', sa.String(length=64), nullable=True),
    sa.Column('reported_doctor_id', sa.String(length=50), nullable=True),
    sa.Column('reported_name', sa.String(length=200), nullable=False),
    sa.Column('reason', sa.String(length=40), nullable=False),
    sa.Column('details', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='open', nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('reviewed_by', diffdx.db.types.GUID(), nullable=True),
    sa.Column('admin_note', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['reporter_user_id'], ['users.id'], name=op.f('fk_message_reports_reporter_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], name=op.f('fk_message_reports_reviewed_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_message_reports'))
    )
    op.create_index(op.f('ix_message_reports_reporter_user_id'), 'message_reports', ['reporter_user_id'], unique=False)
    op.create_index(op.f('ix_message_reports_thread_id'), 'message_reports', ['thread_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_message_reports_thread_id'), table_name='message_reports')
    op.drop_index(op.f('ix_message_reports_reporter_user_id'), table_name='message_reports')
    op.drop_table('message_reports')
