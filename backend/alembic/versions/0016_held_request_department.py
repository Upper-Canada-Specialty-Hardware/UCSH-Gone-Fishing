"""Held requests keep the new starter's department

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-13

New starters now pick a department instead of a location (#226). Adds
``department`` and lets ``location`` be empty; rows held before keep their
location.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode so the same file works on the local SQLite database.
    with op.batch_alter_table("held_requests") as batch:
        batch.add_column(sa.Column("department", sa.String(), nullable=True))
        batch.alter_column("location", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("held_requests") as batch:
        batch.alter_column("location", existing_type=sa.String(), nullable=False)
        batch.drop_column("department")
