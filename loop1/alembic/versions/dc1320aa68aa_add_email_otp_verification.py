"""add email OTP verification

Revision ID: dc1320aa68aa
Revises: 9ef2c0edf03e
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import diffdx.db.types


# revision identifiers, used by Alembic.
revision: str = 'dc1320aa68aa'
down_revision: Union[str, None] = '9ef2c0edf03e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows (already-registered patients, seeded doctors) default
    # to verified — only new patient registrations explicitly pass False
    # going forward. No regression for anyone who could already log in.
    op.add_column('users', sa.Column('email_verified', sa.Boolean(), server_default=sa.true(), nullable=False))
    op.create_table('email_otps',
        sa.Column('user_id', diffdx.db.types.GUID(), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_email_otps_user_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', name=op.f('pk_email_otps')),
    )


def downgrade() -> None:
    op.drop_table('email_otps')
    op.drop_column('users', 'email_verified')
