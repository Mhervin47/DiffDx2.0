"""add deleted_at to users

Revision ID: 35727671c08d
Revises: 7834f7f6fed4
Create Date: 2026-09-16 17:01:50.862170

Self-service account deactivation (patient or doctor) — a single terminal
flag on the identity row itself, not a separate table, since it's a flag
not a log. Setting it means: name/email/password_hash scrubbed in place,
login disabled, but the row itself is NOT deleted — every clinical/
appointment/session/message row that references this user_id stays
exactly as it was. A later, separate, admin-only full purge (actual row
deletion) is unaffected by this column and unchanged by this migration —
see diffdx.dsr_erasure.

Autogenerate also proposed `op.drop_table('store')` here — the same known
trap every other migration touching `db/models/` in this repo has hit:
`store` is the legacy blob-store table (diffdx.legacy_store), managed by
its own raw sqlite3/psycopg2 connections outside SQLAlchemy's metadata
entirely, so Alembic's autogenerate always sees it as "not in the ORM
models" and offers to drop it. It must never actually be dropped — doing
so would destroy `store['appointments']`/`store['messages']`/
`store['session_report:{id}']`, still actively read and written by
existing routers. Removed from both upgrade() and downgrade() below,
same as every prior migration that hit this.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '35727671c08d'
down_revision: Union[str, None] = '7834f7f6fed4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Naive datetime, matching User.created_at/updated_at on this same
    # table — see diffdx.dsr_erasure's module docstring on this codebase's
    # naive-vs-aware split (User.* columns are naive; DiagnosticSession.*/
    # Appointment.cancelled_at are aware).
    op.add_column('users', sa.Column('deleted_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'deleted_at')
