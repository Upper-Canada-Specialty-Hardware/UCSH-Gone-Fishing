"""The rule date on requests, and no more stored booking date (#543)

Which pot pays for a leave, and whether it can be booked, now comes from the date
the request was created or last had its dates or days edited, not from one date
stored per person. ``requests.rules_on`` holds that edit date: null means the
created date, so no row needs filling in. ``staff.request_allow_date`` is dropped;
the history rows that mention it stay, as a record of what it once said.

Postgres only: SQLite (tests) builds its schema from the models with
``create_all``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``requests.rules_on`` and drop ``staff.request_allow_date``."""
    bind = op.get_bind()                                       # branch on the dialect
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite builds from the models via create_all
    op.add_column("requests", sa.Column("rules_on", sa.Date(), nullable=True))
    op.drop_column("staff", "request_allow_date")


def downgrade() -> None:
    """Remove ``rules_on`` and add the old column back empty: its values are not kept."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.add_column("staff", sa.Column("request_allow_date", sa.Date(), nullable=True))
    op.drop_column("requests", "rules_on")
