"""The three request lists (leave, overtime, carry-over/payout) in Postgres.

Each table stands in for one SharePoint list and behaves like it: a row is one
list item, ``id`` is its item id, and ``fields`` holds every column value under
its SharePoint column name, exactly as the app reads and writes them through
Graph today. Keeping the whole field set in one JSON column (rather than one
database column per SharePoint column) means no field the app relies on can be
lost in the move, and the request services keep working unchanged.

A request copied from SharePoint keeps its SharePoint item id as ``id``, so
approval links, SMS replies and approval state that already name that id keep
working. ``sp_item_id`` records which SharePoint item a row came from: the copy
sets it, and so does the Microsoft Form fallback, which moves each new Form
item into Postgres under a new id. A request made in the app has none.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class RequestItemMixin:
    """The columns every request table shares; one table per SharePoint list."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)      # the item id
    fields: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)              # SharePoint column name -> value
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)    # Graph createdDateTime
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)   # Graph lastModifiedDateTime
    sp_item_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)  # source SharePoint item, if any


class LeaveRequestItem(RequestItemMixin, Base):
    """An item of the Leave Requests list."""

    __tablename__ = "leave_requests"


class OvertimeRequestItem(RequestItemMixin, Base):
    """An item of the Overtime Requests list."""

    __tablename__ = "overtime_requests"


class CarryoverPayoutRequestItem(RequestItemMixin, Base):
    """An item of the Carry-over / Payout list."""

    __tablename__ = "carryover_payout_requests"
