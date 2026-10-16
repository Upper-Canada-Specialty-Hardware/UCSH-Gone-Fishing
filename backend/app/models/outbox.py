"""Every message the app owes someone, queued for one sender to deliver (#326).

Instead of sending an email or SMS inline (where a crash or a retry could send
it twice or not at all), each message is written here in the same transaction
as the request change that calls for it, and a single background sender drains
the queue. ``idempotency_key`` makes a message land at most once; ``status``,
``attempts`` and ``next_attempt_at`` drive the retry schedule. The rendered
``body`` is never returned by any API.
"""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import request_vocab as vocab
from app.models.mixins import utcnow


class Outbox(Base):
    """One queued message waiting to be sent."""

    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(vocab.in_clause("channel", vocab.OUTBOX_CHANNELS), name="ck_outbox_channel"),
        CheckConstraint(vocab.in_clause("status", vocab.OUTBOX_STATUSES), name="ck_outbox_status"),
        # The sender polls due messages: by status, soonest next attempt first.
        Index("ix_outbox_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The request and event this message is for; null for a message with neither.
    request_id: Mapped[int | None] = mapped_column(ForeignKey("requests.id"), nullable=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("request_events.id"), nullable=True)
    message_type: Mapped[str] = mapped_column(String, nullable=False)        # manager_approval, receipt, result, ...
    channel: Mapped[str] = mapped_column(String, nullable=False)             # email or sms
    recipient: Mapped[str] = mapped_column(String, nullable=False)           # address or phone number
    subject: Mapped[str | None] = mapped_column(String, nullable=True)       # email subject, null for SMS
    body: Mapped[str] = mapped_column(Text, nullable=False)                  # rendered HTML or SMS text; never returned by an API

    # --- delivery ---
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")   # pending/sending/sent/failed/dropped
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String, nullable=True)   # the provider's id for the sent message
    email_api_log_id: Mapped[int | None] = mapped_column(Integer, nullable=True)      # the email_api_log row, once sent
    # The step key plus channel and recipient; unique so a message sends once.
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
