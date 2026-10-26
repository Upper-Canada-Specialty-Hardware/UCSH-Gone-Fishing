"""Lifecycle columns on held_requests (#443, #445)

Extends the ``held_requests`` table so one held request can be handed between
supervisors and cancelled with a reason that can be undone:

- ``supervisors_involved``: every supervisor it has reached, with each outcome,
  so earlier supervisors can be told when it moves or is handled (#443).
- ``cancel_reason`` and ``status_before_cancel``: a cancel records why and can be
  reversed to the status it held before (#445).

Postgres only: SQLite (tests) builds its schema from the models with
``create_all``, which already carries these columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the lifecycle columns to held_requests."""
    bind = op.get_bind()                                       # branch on the dialect
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite builds from the models via create_all

    op.add_column("held_requests", sa.Column("supervisors_involved", sa.JSON(), nullable=True))
    op.add_column("held_requests", sa.Column("cancel_reason", sa.Text(), nullable=True))
    op.add_column("held_requests", sa.Column("status_before_cancel", sa.String(), nullable=True))


def downgrade() -> None:
    """Drop the lifecycle columns."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_column("held_requests", "status_before_cancel")
    op.drop_column("held_requests", "cancel_reason")
    op.drop_column("held_requests", "supervisors_involved")
