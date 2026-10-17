"""Give the request child tables ON DELETE rules, and add outbox.claimed_at

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17

Finding 4 (admin delete on Postgres): the child tables of ``requests``
(``request_events``, ``request_changes``, ``balance_ledger`` and ``outbox``)
were created in 0019 with plain foreign keys and no ON DELETE rule, so deleting
a request raised a foreign-key violation on Postgres and the admin delete always
failed. This revision drops those foreign keys and recreates them with:

- ``request_events`` and ``request_changes`` -> ON DELETE CASCADE (the timeline
  and the field history have no meaning without their request, so they go too);
- ``balance_ledger.request_id`` and ``outbox.request_id`` / ``outbox.event_id``
  -> ON DELETE SET NULL (the balance history and the record that a message was
  sent survive the delete, with the link cleared).

Finding 6 (stuck "sending" outbox rows): adds ``outbox.claimed_at`` so the
worker can tell how long a row has been "sending" and return an abandoned claim
to "pending".

0019 created those foreign keys unnamed. PostgreSQL normally names them
``<table>_<column>_fkey``, but rather than assume it, the upgrade looks up the
actual constraint on each column and drops whatever it is called.
The foreign-key rewrite is Postgres-only: SQLite cannot ALTER a constraint, and
in SQLite (tests) the ON DELETE rules come from the models via create_all, which
the finding-4 test exercises with ``PRAGMA foreign_keys=ON``. The
``claimed_at`` add_column runs on both. Production is Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# A timezone-aware UTC timestamp column, matching the request tables.
_TS = sa.DateTime(timezone=True)

# Each child foreign key to rewrite: the table, the column, the name PostgreSQL
# gave the unnamed 0019 constraint, the explicit name to give the new one, and
# the ON DELETE action. CASCADE where the row is meaningless without its request;
# SET NULL where the row is history that must outlive the request.
_FOREIGN_KEYS = [
    ("balance_ledger", "request_id", "requests", "id",
     "balance_ledger_request_id_fkey", "fk_balance_ledger_request_id", "SET NULL"),
    ("request_events", "request_id", "requests", "id",
     "request_events_request_id_fkey", "fk_request_events_request_id", "CASCADE"),
    ("request_changes", "request_id", "requests", "id",
     "request_changes_request_id_fkey", "fk_request_changes_request_id", "CASCADE"),
    ("outbox", "request_id", "requests", "id",
     "outbox_request_id_fkey", "fk_outbox_request_id", "SET NULL"),
    ("outbox", "event_id", "request_events", "id",
     "outbox_event_id_fkey", "fk_outbox_event_id", "SET NULL"),
]


def upgrade() -> None:
    bind = op.get_bind()                                       # the live connection, to branch on dialect
    # claimed_at runs on both dialects: a plain nullable ADD COLUMN SQLite allows.
    op.add_column("outbox", sa.Column("claimed_at", _TS, nullable=True))
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite: the models carry the ON DELETE rules
    inspector = sa.inspect(bind)                               # read the real constraint names
    for table, column, ref_table, ref_col, old_name, new_name, action in _FOREIGN_KEYS:
        existing = [
            fk["name"] for fk in inspector.get_foreign_keys(table)
            if fk.get("constrained_columns") == [column] and fk.get("referred_table") == ref_table
        ]
        for name in existing or [old_name]:                     # the found name(s), else the usual default
            op.drop_constraint(name, table, type_="foreignkey")    # remove the 0019 no-action foreign key
        op.create_foreign_key(                                    # recreate it with the ON DELETE rule
            new_name, table, ref_table, [column], [ref_col], ondelete=action,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Put the foreign keys back as 0019 had them: no ON DELETE rule, and the
        # default name PostgreSQL would assign an unnamed constraint.
        for table, column, ref_table, ref_col, old_name, new_name, _action in _FOREIGN_KEYS:
            op.drop_constraint(new_name, table, type_="foreignkey")
            op.create_foreign_key(old_name, table, ref_table, [column], [ref_col])
    op.drop_column("outbox", "claimed_at")
