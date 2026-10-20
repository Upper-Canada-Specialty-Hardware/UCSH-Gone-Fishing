"""Widen the balance numerics to 12,4 and extend the finding/job CHECKs

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-18

Two fixes before the production rehearsal (#335, #336, #341):

- Numeric precision. Days, hours, partial_hours, the five staff balances and the
  ledger before/after/change were ``Numeric(8, 2)``, but SharePoint balances carry
  up to four decimals (hours / 8, e.g. 0.1875), so two decimals would round them on
  import. Every such column is widened to ``Numeric(12, 4)``. 0019 and 0020 are left
  as they were (the preview database already ran them); this revision alters the
  columns in place.

- New closed values. The legacy-findings ``case`` CHECK gains ``email_conflict``,
  ``pending_unprocessed`` and ``unknown_status``; the sync-runs ``job`` CHECK gains
  ``sharepoint_check_item`` (the per-item webhook check, kept apart from the
  scheduled sweep). The allowed sets live in ``app.models.request_vocab``, so the
  new CHECKs are rebuilt from there and stay in step with the models.

The column widening and the CHECK rebuild are Postgres-only: SQLite (tests) builds
its schema from the models with ``create_all``, which already carry the wider types
and the new values, and SQLite cannot ALTER a column type or a CHECK. Production is
Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.models import request_vocab as vocab

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The balance columns to widen: (table, column). All move from Numeric(8, 2) to
# Numeric(12, 4) so a four-decimal SharePoint balance imports without rounding.
_NUMERIC_COLUMNS = [
    ("requests", "partial_hours"),
    ("requests", "days"),
    ("requests", "hours"),
    ("balance_ledger", "change"),
    ("balance_ledger", "before"),
    ("balance_ledger", "after"),
    ("staff", "vacation_entitlement"),
    ("staff", "sick_entitlement"),
    ("staff", "vacation_balance"),
    ("staff", "sick_balance"),
    ("staff", "overtime_balance"),
    ("staff", "carry_over"),
    ("staff", "payout"),
]

_OLD_NUM = sa.Numeric(8, 2)
_NEW_NUM = sa.Numeric(12, 4)


def upgrade() -> None:
    bind = op.get_bind()                                       # branch on the dialect
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite builds from the models via create_all
    for table, column in _NUMERIC_COLUMNS:
        op.alter_column(table, column, existing_type=_OLD_NUM, type_=_NEW_NUM)
    # Rebuild the two CHECKs from the current vocab, so the new values are allowed.
    op.drop_constraint("ck_legacy_findings_case", "legacy_findings", type_="check")
    op.create_check_constraint(
        "ck_legacy_findings_case", "legacy_findings", vocab.in_clause('"case"', vocab.FINDING_CASES)
    )
    op.drop_constraint("ck_sync_runs_job", "sync_runs", type_="check")
    op.create_check_constraint(
        "ck_sync_runs_job", "sync_runs", vocab.in_clause("job", vocab.SYNC_JOBS)
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Put the CHECKs back without the new values. Rows carrying a new value would
    # fail the old CHECK, so this only applies to a database that never stored one.
    _OLD_CASES = tuple(
        c for c in vocab.FINDING_CASES
        if c not in ("email_conflict", "pending_unprocessed", "unknown_status")
    )
    _OLD_JOBS = tuple(j for j in vocab.SYNC_JOBS if j != "sharepoint_check_item")
    op.drop_constraint("ck_sync_runs_job", "sync_runs", type_="check")
    op.create_check_constraint("ck_sync_runs_job", "sync_runs", vocab.in_clause("job", _OLD_JOBS))
    op.drop_constraint("ck_legacy_findings_case", "legacy_findings", type_="check")
    op.create_check_constraint(
        "ck_legacy_findings_case", "legacy_findings", vocab.in_clause('"case"', _OLD_CASES)
    )
    for table, column in _NUMERIC_COLUMNS:
        op.alter_column(table, column, existing_type=_NEW_NUM, type_=_OLD_NUM)
