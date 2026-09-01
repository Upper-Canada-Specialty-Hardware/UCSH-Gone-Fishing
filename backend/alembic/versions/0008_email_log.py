"""Add email_log - one row per outbound email attempt

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-01

Auxiliary state, like processing_log and dashboard_link_state - not part of
the SharePoint to Postgres migration. Records every call the app makes to
SMTP2GO (accepted, partially accepted, failed, or skipped for want of an
address) so an admin can look up what was sent to a given person instead of
searching the hosting logs.

Purely additive: creates one empty table. Rows appear as emails go out, so
applying this on its own changes nothing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        # "sent" | "partial" | "failed" | "skipped"
        sa.Column("status", sa.String(length=16), nullable=False),
        # Packed as ",a@x.com,b@x.com," for exact per-address LIKE matching.
        sa.Column("to_addresses", sa.Text(), nullable=False),
        sa.Column("cc_addresses", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        # Staff Directory item id of the main recipient, when known.
        sa.Column("primary_employee_id", sa.String(), nullable=True),
        sa.Column("smtp2go_email_id", sa.String(), nullable=True),
        sa.Column("smtp2go_request_id", sa.String(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Lookups are "this person, newest first": index both halves of that.
    op.create_index(
        op.f("ix_email_log_sent_at"), "email_log", ["sent_at"], unique=False
    )
    op.create_index(
        op.f("ix_email_log_primary_employee_id"),
        "email_log",
        ["primary_employee_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_email_log_primary_employee_id"), table_name="email_log")
    op.drop_index(op.f("ix_email_log_sent_at"), table_name="email_log")
    op.drop_table("email_log")
