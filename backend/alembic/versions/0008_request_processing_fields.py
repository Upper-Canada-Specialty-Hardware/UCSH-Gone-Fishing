"""Add request-processing columns the services write

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-27

0005 ported the three request lists but omitted several fields the approval,
refund, and audit paths write. Served as absent they would make the Postgres
request repository raise on write (unknown field) at cutover, so add them here.
Audited by grepping every dict-key written to a request list across the three
request services and audit_trail.

Per table:
  * leave_requests    — approved_date, new_balances, balance_audit_log
  * overtime_requests — approved_date, balance_audit_log
  * carryover_payout_requests — status, approved_date, new_balance,
    balance_audit_log

`status` is added to carryover_payout because that service both reads and writes
a Status field distinct from its SystemState. `balance_audit_log` holds the JSON
audit trail (AuditTrailBuilder -> write_audit_log) written to all three lists.

All additive and nullable — nothing reads these tables until STORAGE_REQUESTS is
flipped, so applying this changes no behaviour.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # leave_requests
    op.add_column("leave_requests", sa.Column("approved_date", sa.Date(), nullable=True))
    op.add_column("leave_requests", sa.Column("new_balances", sa.String(), nullable=True))
    op.add_column("leave_requests", sa.Column("balance_audit_log", sa.Text(), nullable=True))
    # overtime_requests
    op.add_column("overtime_requests", sa.Column("approved_date", sa.Date(), nullable=True))
    op.add_column("overtime_requests", sa.Column("balance_audit_log", sa.Text(), nullable=True))
    # carryover_payout_requests
    op.add_column("carryover_payout_requests", sa.Column("status", sa.String(), nullable=True))
    op.add_column("carryover_payout_requests", sa.Column("approved_date", sa.Date(), nullable=True))
    op.add_column("carryover_payout_requests", sa.Column("new_balance", sa.String(), nullable=True))
    op.add_column("carryover_payout_requests", sa.Column("balance_audit_log", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("carryover_payout_requests", "balance_audit_log")
    op.drop_column("carryover_payout_requests", "new_balance")
    op.drop_column("carryover_payout_requests", "approved_date")
    op.drop_column("carryover_payout_requests", "status")
    op.drop_column("overtime_requests", "balance_audit_log")
    op.drop_column("overtime_requests", "approved_date")
    op.drop_column("leave_requests", "balance_audit_log")
    op.drop_column("leave_requests", "new_balances")
    op.drop_column("leave_requests", "approved_date")
