"""One field changed on one request, kept as history (#332).

Separate from balance_ledger (which records pot movements) and request_events
(the timeline): this is the field-level audit of a request itself, written
whenever the typed adapter or an admin edit changes a stored value. Both the
old and the new value are kept as JSON so a number, string or null all
round-trip, and the row says who changed it, on which channel, and why.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class RequestChange(Base):
    """One field changed on one request."""

    __tablename__ = "request_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ON DELETE CASCADE: the field-change history goes with the request when an
    # admin deletes it (#282, finding 4); it describes that request and nothing else.
    request_id: Mapped[int] = mapped_column(
        ForeignKey("requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field: Mapped[str] = mapped_column(String, nullable=False)          # the SharePoint column name changed
    old_value: Mapped[object] = mapped_column(JSON, nullable=True)       # value before (JSON: any shape)
    new_value: Mapped[object] = mapped_column(JSON, nullable=True)       # value after
    actor: Mapped[str | None] = mapped_column(String, nullable=True)     # who changed it
    channel: Mapped[str | None] = mapped_column(String, nullable=True)   # where the change came from
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)      # why, when there is a reason
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
