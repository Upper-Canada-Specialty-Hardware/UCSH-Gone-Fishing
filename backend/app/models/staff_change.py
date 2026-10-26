"""An append-only history of every change HR makes to a person's numbers.

Each row is one field changed on one staff record: a vacation balance edited,
an entitlement corrected, a carry-over adjusted. The admin dashboard writes a
row per field whenever HR saves a change, so the full story of how someone's
entitlements and balances reached today's values can be read back later
(employees ask, and payroll disputes need it).

The person's name is stored on the row as it was at the time of the change, so
the history still reads correctly after a later rename. ``source`` says where
the change came from: the admin dashboard today, and "sharepoint" once a sync
records edits made directly in the Staff Directory.

Rows are only ever inserted, never updated or deleted: the table is the record
of what happened, so editing it would be rewriting history.

Auxiliary state, like processing_log and held_requests. Not a SharePoint list.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class StaffChange(Base):
    """One field changed on one staff record, kept forever."""

    __tablename__ = "staff_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # row id, own sequence
    # Staff Directory item id as a string -> the person the change is about;
    # indexed because every read filters history by one person.
    employee_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Their name when the change happened -> history reads well after a rename.
    employee_name: Mapped[str] = mapped_column(String, nullable=False)
    # The SharePoint column name changed, e.g. "CurrentVacationBalance".
    field: Mapped[str] = mapped_column(String, nullable=False)
    # Value before the change; JSON so a number, string or None all round-trip.
    old_value: Mapped[object] = mapped_column(JSON, nullable=True)
    # Value after the change; same JSON shape as old_value.
    new_value: Mapped[object] = mapped_column(JSON, nullable=True)
    # Why the change was made -> free text; nullable because an imported
    # SharePoint version often carries no comment to explain it (#448).
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who or what made the change: "HR", a manager's name, "system", or the
    # SharePoint editor's name for an imported row. Null on older rows, where the
    # reader falls back to ``source`` (#447).
    actor: Mapped[str | None] = mapped_column(String, nullable=True)
    # The SharePoint version label an imported row came from (e.g. "1.0"); null
    # for a change the app recorded directly (#447).
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    # Where the change came from; the admin dashboard now, "sharepoint" later.
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="admin_dashboard")
    # When it happened (UTC, tz-aware); indexed because reads order by it newest first.
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
