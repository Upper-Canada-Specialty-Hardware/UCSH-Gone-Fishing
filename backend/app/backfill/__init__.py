"""SharePoint -> Postgres backfill / verify tooling (migration PR D).

A one-off maintenance command that copies the current SharePoint list data into
the Postgres business tables (Alembic 0005), keyed on ``sp_item_id`` so it is
idempotent, and a read-only ``verify`` mode that diffs both sides to prove parity
BEFORE any domain's read path is flipped to Postgres. It flips no storage flags
and changes no production reads — running it is a deliberate, separate step.

Run it with ``python -m app.backfill`` (see ``__main__``).
"""
