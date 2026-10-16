"""Add the typed request tables, ledger, timeline, outbox and sync records

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-16

The Postgres home the request migration moves into (#321-#341): one typed
``requests`` table for all three kinds, a ``balance_ledger`` of pot movements, a
``request_events`` timeline, a ``request_changes`` field audit, an ``outbox`` of
messages to send, a per-kind ``request_counters`` id source, and the
``sharepoint_seen`` / ``sync_runs`` / ``legacy_findings`` records the importer
and the post-switch sweep write. The closed value sets come from
``app.models.request_vocab`` so the CHECK constraints here match the models and
the services exactly.

The JSON request tables from 0013 (leave_requests and so on) are left in place;
removing them is a later decision. Runs on both Postgres and SQLite.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.models import request_vocab as vocab

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Days and hours: two decimals, handed to Python as float not Decimal.
_NUM = sa.Numeric(8, 2, asdecimal=False)
# A timezone-aware UTC timestamp column.
_TS = sa.DateTime(timezone=True)


def _check(name: str, text: str) -> sa.CheckConstraint:
    """A named CHECK constraint from a SQL fragment.

    Args:
        name: The constraint name.
        text: The boolean SQL expression.

    Returns:
        The CheckConstraint to pass to create_table.
    """
    return sa.CheckConstraint(text, name=name)


def upgrade() -> None:
    # The per-kind id counter new requests draw item_id from.
    op.create_table(
        "request_counters",
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("last_item_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("kind"),
    )

    # The one typed table every request kind lives in.
    op.create_table(
        "requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("sp_item_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("employee_id", sa.String(), nullable=False),
        sa.Column("employee_name", sa.String(), nullable=True),
        sa.Column("employee_email", sa.String(), nullable=True),
        sa.Column("manager_id", sa.String(), nullable=True),
        sa.Column("manager_name", sa.String(), nullable=True),
        sa.Column("employment_type", sa.String(), nullable=True),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("submitted_channel", sa.String(), nullable=True),
        sa.Column("leave_type", sa.String(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("partial_hours", _NUM, nullable=True),
        sa.Column("days", _NUM, nullable=True),
        sa.Column("hours", _NUM, nullable=True),
        sa.Column("request_option", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("bereavement_relation", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("approval_version", sa.Integer(), nullable=False),
        sa.Column("decided_at", _TS, nullable=True),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_channel", sa.String(), nullable=True),
        sa.Column("balance_state", sa.String(), nullable=False),
        sa.Column("legacy", sa.Boolean(), nullable=False),
        sa.Column("reminder_count", sa.Integer(), nullable=False),
        sa.Column("last_reminder_at", _TS, nullable=True),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "item_id", name="uq_requests_kind_item_id"),
        sa.UniqueConstraint("kind", "sp_item_id", name="uq_requests_kind_sp_item_id"),
        _check("ck_requests_kind", vocab.in_clause("kind", vocab.KINDS)),
        _check("ck_requests_status", vocab.in_clause("status", vocab.STATUSES)),
        _check("ck_requests_balance_state", vocab.in_clause("balance_state", vocab.BALANCE_STATES)),
        _check(
            "ck_requests_leave_type",
            f"leave_type IS NULL OR {vocab.in_clause('leave_type', vocab.LEAVE_TYPES_ALLOWED)}",
        ),
        _check(
            "ck_requests_employment_type",
            f"employment_type IS NULL OR {vocab.in_clause('employment_type', vocab.EMPLOYMENT_TYPES)}",
        ),
        _check(
            "ck_requests_request_option",
            f"request_option IS NULL OR {vocab.in_clause('request_option', vocab.REQUEST_OPTIONS)}",
        ),
        _check(
            "ck_requests_leave_fields",
            "kind != 'leave' OR (leave_type IS NOT NULL AND start_date IS NOT NULL)",
        ),
        _check(
            "ck_requests_overtime_fields",
            "kind != 'overtime' OR (start_date IS NOT NULL AND hours > 0)",
        ),
        _check(
            "ck_requests_carryover_fields",
            "kind != 'carryover_payout' OR (request_option IS NOT NULL AND days > 0)",
        ),
        _check(
            "ck_requests_date_order",
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
        ),
        _check("ck_requests_days_nonneg", "days IS NULL OR days >= 0"),
        _check("ck_requests_hours_nonneg", "hours IS NULL OR hours >= 0"),
        _check("ck_requests_partial_hours_nonneg", "partial_hours IS NULL OR partial_hours >= 0"),
    )
    op.create_index("ix_requests_employee_id", "requests", ["employee_id"])
    op.create_index("ix_requests_manager_id", "requests", ["manager_id"])
    op.create_index("ix_requests_employee_created", "requests", ["employee_id", "created_at"])
    op.create_index("ix_requests_manager_status", "requests", ["manager_id", "status"])
    op.create_index("ix_requests_status_created", "requests", ["status", "created_at"])
    op.create_index("ix_requests_kind_start_date", "requests", ["kind", "start_date"])

    # Every balance pot movement, one row each.
    op.create_table(
        "balance_ledger",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("employee_id", sa.String(), nullable=False),
        sa.Column("pot", sa.String(), nullable=False),
        sa.Column("change", _NUM, nullable=False),
        sa.Column("before", _NUM, nullable=False),
        sa.Column("after", _NUM, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"]),
        sa.UniqueConstraint("idempotency_key", name="uq_balance_ledger_idempotency_key"),
        _check("ck_balance_ledger_pot", vocab.in_clause("pot", vocab.POTS)),
        _check("ck_balance_ledger_channel", vocab.in_clause("channel", vocab.CHANNELS)),
    )
    op.create_index("ix_balance_ledger_employee_id", "balance_ledger", ["employee_id"])
    op.create_index("ix_balance_ledger_employee_created", "balance_ledger", ["employee_id", "created_at"])

    # The request timeline; id is also the live-stream position.
    op.create_table(
        "request_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.String(), nullable=False),
        sa.Column("manager_id", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"]),
        sa.UniqueConstraint("idempotency_key", name="uq_request_events_idempotency_key"),
        _check("ck_request_events_type", vocab.in_clause("type", vocab.EVENT_TYPES)),
        _check("ck_request_events_channel", vocab.in_clause("channel", vocab.CHANNELS)),
    )
    op.create_index("ix_request_events_request_id", "request_events", ["request_id"])

    # The field-level audit of the request itself.
    op.create_table(
        "request_changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("old_value", sa.JSON(), nullable=True),
        sa.Column("new_value", sa.JSON(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("channel", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("changed_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"]),
    )
    op.create_index("ix_request_changes_request_id", "request_changes", ["request_id"])

    # The queue of messages to send.
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("message_type", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("recipient", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", _TS, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("email_api_log_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("sent_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["request_events.id"]),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_idempotency_key"),
        _check("ck_outbox_channel", vocab.in_clause("channel", vocab.OUTBOX_CHANNELS)),
        _check("ck_outbox_status", vocab.in_clause("status", vocab.OUTBOX_STATUSES)),
    )
    op.create_index("ix_outbox_status_next_attempt", "outbox", ["status", "next_attempt_at"])

    # What the SharePoint sweep decided about each item version.
    op.create_table(
        "sharepoint_seen",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("sp_item_id", sa.String(), nullable=False),
        sa.Column("sp_version", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("seen_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "sp_item_id", "sp_version", name="uq_sharepoint_seen_version"),
        _check("ck_sharepoint_seen_outcome", vocab.in_clause("outcome", vocab.SEEN_OUTCOMES)),
    )

    # One run of a sync or sweep job.
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job", sa.String(), nullable=False),
        sa.Column("started_at", _TS, nullable=False),
        sa.Column("finished_at", _TS, nullable=True),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        _check("ck_sync_runs_job", vocab.in_clause("job", vocab.SYNC_JOBS)),
    )

    # Reconciliation findings for HR to resolve. "case" is a SQL keyword, so it
    # is quoted in the CHECK; the column itself is quoted automatically.
    op.create_table(
        "legacy_findings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("case", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", _TS, nullable=True),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_legacy_findings_fingerprint"),
        _check("ck_legacy_findings_subject", vocab.in_clause("subject", vocab.FINDING_SUBJECTS)),
        _check("ck_legacy_findings_case", vocab.in_clause('"case"', vocab.FINDING_CASES)),
        _check("ck_legacy_findings_kind", f"kind IS NULL OR {vocab.in_clause('kind', vocab.KINDS)}"),
        _check("ck_legacy_findings_status", vocab.in_clause("status", vocab.FINDING_STATUSES)),
    )


def downgrade() -> None:
    # Reverse order: a table's foreign keys point at one created before it.
    op.drop_table("legacy_findings")
    op.drop_table("sync_runs")
    op.drop_table("sharepoint_seen")
    op.drop_table("outbox")
    op.drop_table("request_changes")
    op.drop_table("request_events")
    op.drop_table("balance_ledger")
    op.drop_table("requests")
    op.drop_table("request_counters")
