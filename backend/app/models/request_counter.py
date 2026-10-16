"""The per-kind id counter new requests draw their item_id from (#321).

A request is known to people and links by ``(kind, item_id)``. Imported
SharePoint items keep their SharePoint id as item_id; a request made in the app
takes the next number for its kind from here. One row per kind holds the last
id handed out, bumped atomically by ``request_journal.next_item_id``.

Auxiliary bookkeeping. Not a SharePoint list.
"""
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RequestCounter(Base):
    """The last item_id given out for one request kind."""

    __tablename__ = "request_counters"

    # The request kind this counter is for; one row each, so kind is the key.
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    # The highest item_id handed out so far; the next new request gets this + 1.
    last_item_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
