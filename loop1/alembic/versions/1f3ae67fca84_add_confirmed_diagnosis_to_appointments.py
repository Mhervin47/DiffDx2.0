"""add confirmed_diagnosis to appointments

Revision ID: 1f3ae67fca84
Revises: c46f6274d2ea
Create Date: 2026-09-13 00:00:00.000000

Doctor-confirmed diagnosis disclosure (see DOCTOR_CONFIRMED_DIAGNOSIS_PLAN.md).
Same shape as the doctor_summary/summary_updated_at pair added in
9ef2c0edf03e — a doctor-authored value distinct from the AI's own
primary_diagnosis column, never auto-derived from it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1f3ae67fca84'
down_revision: Union[str, None] = 'c46f6274d2ea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('appointments', sa.Column('confirmed_diagnosis', sa.String(length=500), nullable=True))
    op.add_column('appointments', sa.Column('diagnosis_confirmed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('appointments', 'diagnosis_confirmed_at')
    op.drop_column('appointments', 'confirmed_diagnosis')
