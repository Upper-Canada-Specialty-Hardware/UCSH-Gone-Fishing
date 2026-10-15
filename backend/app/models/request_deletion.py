from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class RequestDeletion(Base):
    """A record of a request an admin deleted from the requests screen (#282).

    Deleting a request removes the list item for good, so what it was and why
    it was deleted must be kept somewhere. This table is that record: the whole
    item (every field, as JSON) at the moment of deletion, the reason the admin
    typed, and whether its balance change was refunded first. Auxiliary state,
    like held_requests; not a SharePoint list.
    """

    __tablename__ = "request_deletions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # "leave", "overtime" or "carryover-payout": which list the item came from.
    request_type: Mapped[str] = mapped_column(String, nullable=False)
    # The deleted item's id (SharePoint or Postgres), kept as a string like Graph sends it.
    list_item_id: Mapped[str] = mapped_column(String, nullable=False)
    # The item's full field set at deletion, so nothing is lost when the item goes.
    fields: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Why the admin deleted it; required, never blank (the endpoint enforces it).
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # True when an approved item's balance change was reversed before deleting.
    refunded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # When it was deleted, UTC (aware; round-trips as timestamptz on Postgres).
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
