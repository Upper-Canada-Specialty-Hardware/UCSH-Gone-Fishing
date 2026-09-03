"""Replace email_log with email_api_log - the SMTP2GO request/response record

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-03

Auxiliary state, like processing_log and dashboard_link_state - not part of
the SharePoint to Postgres migration.

The email_log table from 0008 stored the backend's reading of each send (a
status word, two ids, error text on failure) but not SMTP2GO's answer itself,
so a row could not show what the API actually said. It is replaced by two
tables:

* email_api_log: one row per HTTP call to SMTP2GO, holding the redacted
  request (no api_key, no HTML body) and the response exactly as received,
  plus the derived outcome and counts.
* email_api_log_recipient: one row per To/CC address on each call, normalised
  and indexed, so the admin lookup by person is an exact join.

The old table held one day of rows in production when this was written; they
are dropped, not copied, because they lack the response body this record
exists for.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Retire the interpreted log.
    op.drop_index(op.f("ix_email_log_primary_employee_id"), table_name="email_log")
    op.drop_index(op.f("ix_email_log_sent_at"), table_name="email_log")
    op.drop_table("email_log")

    # One row per SMTP2GO call: the request we made and the answer we got.
    op.create_table(
        "email_api_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        # the request
        sa.Column("request_url", sa.String(), nullable=False),
        sa.Column("sender", sa.String(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        # payload minus api_key; html_body replaced by byte length + sha256
        sa.Column("request_json", sa.Text(), nullable=False),
        # the answer
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("no_response_reason", sa.Text(), nullable=True),
        # derived: accepted | partially_accepted | rejected | http_error |
        # unreadable_response | no_response | not_attempted
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("succeeded_count", sa.Integer(), nullable=True),
        sa.Column("failed_count", sa.Integer(), nullable=True),
        sa.Column("smtp2go_email_id", sa.String(), nullable=True),
        sa.Column("smtp2go_request_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Lookups are newest-first inside a window.
    op.create_index(
        op.f("ix_email_api_log_attempted_at"), "email_api_log", ["attempted_at"], unique=False
    )

    # One row per address per call; the person lookup joins here.
    op.create_table(
        "email_api_log_recipient",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("log_id", sa.Integer(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("field", sa.String(length=4), nullable=False),  # "to" | "cc"
        sa.ForeignKeyConstraint(["log_id"], ["email_api_log.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_email_api_log_recipient_log_id"),
        "email_api_log_recipient",
        ["log_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_api_log_recipient_address"),
        "email_api_log_recipient",
        ["address"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_email_api_log_recipient_address"), table_name="email_api_log_recipient"
    )
    op.drop_index(
        op.f("ix_email_api_log_recipient_log_id"), table_name="email_api_log_recipient"
    )
    op.drop_table("email_api_log_recipient")
    op.drop_index(op.f("ix_email_api_log_attempted_at"), table_name="email_api_log")
    op.drop_table("email_api_log")

    # Restore the 0008 table, empty, so 0008's own downgrade still applies.
    op.create_table(
        "email_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("to_addresses", sa.Text(), nullable=False),
        sa.Column("cc_addresses", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("primary_employee_id", sa.String(), nullable=True),
        sa.Column("smtp2go_email_id", sa.String(), nullable=True),
        sa.Column("smtp2go_request_id", sa.String(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_email_log_sent_at"), "email_log", ["sent_at"], unique=False)
    op.create_index(
        op.f("ix_email_log_primary_employee_id"),
        "email_log",
        ["primary_employee_id"],
        unique=False,
    )
