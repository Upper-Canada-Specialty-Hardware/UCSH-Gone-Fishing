"""Explicit request state machine (#455)

Adds the ``state`` column to ``requests``: the backend-owned position in a
request's lifecycle (getting_ready, waiting_on_approval, approved, rejected,
refunded, cancelled, needs_attention), alongside the untouched ``status``
column. Only ``waiting_on_approval`` can be decided, so a request can no longer
be approved in the gap between its row being saved and its preparation
finishing. See services/request_states.py for the moves.

The column is added nullable, backfilled from each row's status and signals, then
set NOT NULL with a server default of ``getting_ready`` and pinned to the known
values with a CHECK, so every existing row gets the right state and every new row
starts as getting_ready.

This is a schema change, not the SharePoint-to-Postgres data move. down_revision
is "0024" for now; it is re-pointed to "0026" when this branch is combined with
the branches that add 0025 and 0026.

Postgres only: SQLite (tests) builds its schema from the models with
``create_all``, which already carries this column and its CHECK.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``state``, backfill it, then make it NOT NULL with a CHECK."""
    bind = op.get_bind()                                       # branch on the dialect
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite builds from the models via create_all
    # Add it nullable first, so the backfill can fill every existing row.
    op.add_column("requests", sa.Column("state", sa.String(), nullable=True))
    # Backfill: a decided status maps straight across; a pending row is
    # waiting_on_approval when it is ready to be decided (a manager and a real
    # submitted/imported/sent-to-manager event, or a legacy row), else
    # needs_attention (a pending row the old pipeline never finished). Mirrors
    # services/request_states.backfill_state exactly.
    op.execute(
        """
        UPDATE requests SET state = CASE
            WHEN status = 'approved'  THEN 'approved'
            WHEN status = 'rejected'  THEN 'rejected'
            WHEN status = 'refunded'  THEN 'refunded'
            WHEN status = 'cancelled' THEN 'cancelled'
            WHEN status = 'pending' AND (
                legacy = TRUE
                OR (manager_id IS NOT NULL AND EXISTS (
                    SELECT 1 FROM request_events e
                    WHERE e.request_id = requests.id
                      AND e.type IN ('submitted', 'imported', 'sent_to_manager')
                ))
            ) THEN 'waiting_on_approval'
            WHEN status = 'pending' THEN 'needs_attention'
            ELSE 'getting_ready'
        END
        """
    )
    # Now that no row is null, pin the default and the NOT NULL for new rows.
    op.alter_column(
        "requests", "state", existing_type=sa.String(),
        nullable=False, server_default="getting_ready",
    )
    op.create_check_constraint(
        "ck_requests_state", "requests",
        "state IN ('getting_ready', 'waiting_on_approval', 'approved', "
        "'rejected', 'refunded', 'cancelled', 'needs_attention')",
    )


def downgrade() -> None:
    """Drop the CHECK and the ``state`` column."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_requests_state", "requests", type_="check")
    op.drop_column("requests", "state")
