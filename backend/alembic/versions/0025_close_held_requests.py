"""Close the requests still held for new hires (#525)

The new hire path is gone: nobody can be set up from the request page any more,
nothing releases a held request and no reminder goes out. Rows still open
("held", or "failed" / "waiting_site" from a release that did not finish) would
read as waiting forever, so they are marked "cancelled" with the reason in
``last_error``. The table and every row stay as history; nothing is deleted.

Data only, no schema change. The revision id is 0025 so a database that ran an
earlier, unmerged 0025 (held-request columns that are no longer used) still
starts; such a database keeps its rows as they are.

Postgres only: SQLite (tests) builds its schema from the models with
``create_all`` and holds no old rows.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Why the row closed, kept on the row so the history says so.
CLOSED_NOTE = "Closed: requests are no longer held for new hires (#525)."


def upgrade() -> None:
    """Mark every still-open held request cancelled, with the reason."""
    bind = op.get_bind()                                       # branch on the dialect
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite: no old rows to close
    bind.execute(
        sa.text(
            "UPDATE held_requests SET status = 'cancelled', last_error = :note "
            "WHERE status IN ('held', 'failed', 'waiting_site')"   # open ones only; released stay released
        ),
        {"note": CLOSED_NOTE},
    )


def downgrade() -> None:
    """Nothing to undo: the old statuses are not kept, and nothing reads them."""
