"""Add the admin dashboard password

Revision ID: 0015
Revises: 0013
Create Date: 2026-10-13

One row holding the password that opens the admin dashboard, seeded once
with 123 for HR to change. A developer may overwrite ``password`` with any
plain value; it is hashed at the next sign-in. Deleting the row locks the
dashboard until a row is added again: nothing re-seeds it.

Numbered 0015 because 0014 (app_settings) is on an open branch. Whichever
of the two reaches master second points its down_revision at the other.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_password",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("password", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Seeded here only, so a deleted row never comes back as 123.
    # The id comes from the table's own sequence, so a developer's later
    # INSERT without an id does not collide with it.
    op.execute(
        "INSERT INTO admin_password (password, updated_at) VALUES ('123', CURRENT_TIMESTAMP)"
    )


def downgrade() -> None:
    op.drop_table("admin_password")
