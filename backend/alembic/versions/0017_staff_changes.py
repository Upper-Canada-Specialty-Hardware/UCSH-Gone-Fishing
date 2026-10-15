"""Add staff_changes: the history of edits to entitlements and balances

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-14

Auxiliary state, like processing_log and dashboard_link_state. One row per
field changed on a staff record (a balance edited, an entitlement corrected),
written by the admin dashboard so the history of how someone's numbers reached
today's values can be read back. Rows are only ever inserted.

Purely additive: creates one empty table with an index on the person it is
about and on when the change happened, the two columns every read uses. This
schema change applies on both SQLite (the test database) and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("employee_id", sa.String(), nullable=False),        # who the change is about
        sa.Column("employee_name", sa.String(), nullable=False),      # their name at the time
        sa.Column("field", sa.String(), nullable=False),              # SharePoint column changed
        sa.Column("old_value", sa.JSON(), nullable=True),            # value before, any JSON type
        sa.Column("new_value", sa.JSON(), nullable=True),            # value after, any JSON type
        sa.Column("reason", sa.Text(), nullable=False),              # why HR made the change
        sa.Column("source", sa.String(length=24), nullable=False),  # "admin_dashboard" or "sharepoint"
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),  # when it happened (UTC)
        sa.PrimaryKeyConstraint("id"),
    )
    # Every read filters by one person and orders by time, so both get an index.
    op.create_index(
        op.f("ix_staff_changes_employee_id"), "staff_changes", ["employee_id"], unique=False
    )
    op.create_index(
        op.f("ix_staff_changes_changed_at"), "staff_changes", ["changed_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_staff_changes_changed_at"), table_name="staff_changes")
    op.drop_index(op.f("ix_staff_changes_employee_id"), table_name="staff_changes")
    op.drop_table("staff_changes")
