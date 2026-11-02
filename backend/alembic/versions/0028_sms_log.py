"""Text log tables: sms_api_log and sms_reply_log (#494)

Adds the two tables behind the admin Text Log, the SMS counterpart of the email
log. ``sms_api_log`` holds one row per Twilio send attempt (with the send answer
and, filled in later, Twilio's delivery status), and ``sms_reply_log`` holds one
row per inbound text and what the webhook handler did with it. Both carry the
full phone number on purpose (owner ruling); stdout stays masked.

Postgres only: SQLite (tests) builds its schema from the models with
``create_all``, which already carries these tables.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create ``sms_api_log`` and ``sms_reply_log`` with their lookup indexes."""
    bind = op.get_bind()                                       # branch on the dialect
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite builds from the models via create_all

    # One row per Twilio send attempt; delivery_* columns are updated later by
    # the status callback.
    op.create_table(
        "sms_api_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="other"),
        sa.Column("request_kind", sa.String(length=32), nullable=True),
        sa.Column("request_item_id", sa.String(), nullable=True),
        sa.Column("outbox_id", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("inbound_reply_id", sa.Integer(), nullable=True),
        sa.Column("to_number", sa.String(), nullable=False),
        sa.Column("to_digits", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("from_number", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("no_response_reason", sa.Text(), nullable=True),
        sa.Column("twilio_sid", sa.String(), nullable=True),
        sa.Column("twilio_status", sa.String(length=24), nullable=True),
        sa.Column("error_code", sa.String(length=16), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("delivery_status", sa.String(length=24), nullable=True),
        sa.Column("delivery_error_code", sa.String(length=16), nullable=True),
        sa.Column("delivery_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_history", sa.Text(), nullable=True),
    )
    op.create_index("ix_sms_api_log_attempted_at", "sms_api_log", ["attempted_at"])
    op.create_index("ix_sms_api_log_to_digits", "sms_api_log", ["to_digits"])
    op.create_index("ix_sms_api_log_twilio_sid", "sms_api_log", ["twilio_sid"])
    op.create_index("ix_sms_api_log_inbound_reply_id", "sms_api_log", ["inbound_reply_id"])

    # One row per inbound text and the branch the handler took.
    op.create_table(
        "sms_reply_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_sid", sa.String(), nullable=True),
        sa.Column("from_number", sa.String(), nullable=True),
        sa.Column("from_digits", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("to_number", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("signature_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("request_type", sa.String(length=32), nullable=True),
        sa.Column("item_id", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("sender_staff_id", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=False, server_default="error"),
        sa.Column("error_text", sa.Text(), nullable=True),
    )
    op.create_index("ix_sms_reply_log_received_at", "sms_reply_log", ["received_at"])
    op.create_index("ix_sms_reply_log_message_sid", "sms_reply_log", ["message_sid"])
    op.create_index("ix_sms_reply_log_from_digits", "sms_reply_log", ["from_digits"])


def downgrade() -> None:
    """Drop both text-log tables and their indexes."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_index("ix_sms_reply_log_from_digits", table_name="sms_reply_log")
    op.drop_index("ix_sms_reply_log_message_sid", table_name="sms_reply_log")
    op.drop_index("ix_sms_reply_log_received_at", table_name="sms_reply_log")
    op.drop_table("sms_reply_log")
    op.drop_index("ix_sms_api_log_inbound_reply_id", table_name="sms_api_log")
    op.drop_index("ix_sms_api_log_twilio_sid", table_name="sms_api_log")
    op.drop_index("ix_sms_api_log_to_digits", table_name="sms_api_log")
    op.drop_index("ix_sms_api_log_attempted_at", table_name="sms_api_log")
    op.drop_table("sms_api_log")
