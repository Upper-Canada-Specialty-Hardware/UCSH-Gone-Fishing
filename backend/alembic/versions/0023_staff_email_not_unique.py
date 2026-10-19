"""Let two staff records share one email

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-18

Some mailboxes genuinely belong to two Staff Directory records: a shared desk
(Accounts Payable) and the employee who reads it, or a person and a test record.
The unique constraint from 0020 forced the staff import to blank the email on
the second record, which left that record unable to sign in or be matched. The
constraint is dropped; the plain ``ix_staff_email`` index stays, so lookups by
email are still a direct hit. The request page asks a person with a shared email
which record they are.

Postgres only: SQLite (tests) builds its schema from the models with
``create_all``, and the model no longer declares the column unique.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the unique constraint on staff.email; keep its index."""
    bind = op.get_bind()                                       # branch on the dialect
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite builds from the models via create_all
    op.execute("ALTER TABLE staff DROP CONSTRAINT IF EXISTS uq_staff_email")   # safe if already gone


def downgrade() -> None:
    """Put the unique constraint back.

    Fails while two records still share an email; blank one of them first.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.create_unique_constraint("uq_staff_email", "staff", ["email"])
