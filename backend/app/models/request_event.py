"""The append-only timeline of everything that happened to a request (#323).

Each row is one event: submitted, sent to the manager, approved, result sent,
and so on. ``id`` is an autoincrement sequence, so it doubles as the live
stream position a reader can page from. The request's kind, item_id,
employee_id and manager_id are copied onto the row so a stream can be filtered
by person or kind without joining back to ``requests``. ``idempotency_key``
makes each step land at most once across the three approval channels.
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import request_vocab as vocab
from app.models.mixins import utcnow


class RequestEvent(Base):
    """One event on one request's timeline."""

    __tablename__ = "request_events"
    __table_args__ = (
        CheckConstraint(vocab.in_clause("type", vocab.EVENT_TYPES), name="ck_request_events_type"),
        CheckConstraint(vocab.in_clause("channel", vocab.CHANNELS), name="ck_request_events_channel"),
    )

    # Autoincrement id, which is also the live-stream position readers page from.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), nullable=False, index=True)
    # Denormalised from the request so a stream can filter without a join.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    employee_id: Mapped[str] = mapped_column(String, nullable=False)
    manager_id: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)               # the event type
    channel: Mapped[str] = mapped_column(String, nullable=False)            # where it came from
    actor: Mapped[str | None] = mapped_column(String, nullable=True)        # who caused it
    # What a timeline line needs, e.g. {"to": "sms", "manager": "Dave"}.
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # The step key; unique so the same step never records twice.
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
