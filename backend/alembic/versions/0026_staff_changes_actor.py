"""staff_changes: who made the change, and which SharePoint version it came from

Revision ID: 0026
Revises: 0024
Create Date: 2026-10-25

Adds two nullable columns to ``staff_changes`` and relaxes one:

- ``actor``   - who or what made the change ("HR", a manager's name, "system",
                or the SharePoint editor's display name for an imported row),
- ``version`` - the SharePoint version label an imported row came from (e.g.
                "1.0"),

and makes ``reason`` nullable, because a version imported from SharePoint often
carries no comment to explain it. Existing rows keep working untouched: their
``actor`` and ``version`` are null, and the reader falls back to ``source``.

Postgres only: SQLite (the test database) builds its schema from the models with
``create_all``, which already carries these columns and the relaxed constraint.
This is a schema change, not the SharePoint-to-Postgres data move.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``actor`` and ``version`` to ``staff_changes`` and relax ``reason``."""
    bind = op.get_bind()                                       # branch on the dialect
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite builds from the models via create_all
    op.add_column("staff_changes", sa.Column("actor", sa.String(), nullable=True))     # who/what made the change
    op.add_column("staff_changes", sa.Column("version", sa.String(), nullable=True))   # the SharePoint version label
    op.alter_column("staff_changes", "reason", existing_type=sa.Text(), nullable=True)  # a version may carry no reason


def downgrade() -> None:
    """Drop the two columns and put ``reason`` back to NOT NULL."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("UPDATE staff_changes SET reason = '' WHERE reason IS NULL")             # fill nulls so NOT NULL holds
    op.alter_column("staff_changes", "reason", existing_type=sa.Text(), nullable=False)
    op.drop_column("staff_changes", "version")
    op.drop_column("staff_changes", "actor")
