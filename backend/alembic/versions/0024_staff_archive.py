"""Archive a departed employee (#119)

Adds three nullable columns to ``staff`` so a departed person can be archived
instead of deleted: ``archived_at`` (when they were archived, timezone-aware),
``archived_reason`` (why, required when archiving) and ``archived_by`` (who did
it). An archived person drops out of every staff listing, team, picker, report
and approval route and can no longer make a request, while their past requests
and history stay on record; restore clears all three. ``archived_at`` is indexed
because every default staff listing now filters on "archived_at IS NULL".

Postgres only: SQLite (tests) builds its schema from the models with
``create_all``, which already carries these columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the three archive columns to ``staff`` and index ``archived_at``."""
    bind = op.get_bind()                                       # branch on the dialect
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite builds from the models via create_all
    op.add_column("staff", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("staff", sa.Column("archived_reason", sa.Text(), nullable=True))
    op.add_column("staff", sa.Column("archived_by", sa.String(), nullable=True))
    op.create_index("ix_staff_archived_at", "staff", ["archived_at"])   # default listings filter on it


def downgrade() -> None:
    """Drop the archive columns and their index."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_index("ix_staff_archived_at", table_name="staff")
    op.drop_column("staff", "archived_by")
    op.drop_column("staff", "archived_reason")
    op.drop_column("staff", "archived_at")
