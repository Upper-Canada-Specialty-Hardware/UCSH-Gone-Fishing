"""The Staff Directory in Postgres: people, their links, and the write-back queue (#261).

The Staff Directory SharePoint list is the current source of truth for every
employee: their name and contact details, their office and department, their
employment type, and the five balance pots the approval engine spends. This
module is the typed Postgres home those records move into, one column per
editable Staff Directory column, so a read can be rebuilt into the exact
SharePoint item shape the rest of the app already consumes.

Four tables make up the move:

- ``staff``: one row per person, keyed on the SharePoint item id (``sp_item_id``)
  so an id captured while SharePoint was the source of record still resolves
  after the switch. The person's own Microsoft 365 link (TitleLink) lives here
  because it is their own identity, not a relationship.
- ``staff_supervisors``: the manager and supervisor links. SharePoint holds the
  approving managers in the ``AllManagers`` person column and a single
  ``SupervisorLink``; both are modelled here as links between staff rows, while
  the Microsoft 365 user id and name each link carried are kept verbatim so the
  SharePoint shape rebuilds byte for byte.
- ``locations`` and ``departments``: the two closed choice lists, their own
  lookup tables (the owner's ruling), referenced by ``staff`` by id.
- ``staff_writeback``: the queue of changes made in the app that must be written
  back to the SharePoint list, because the Bulletin Board views, the Microsoft
  Form and other apps keep reading that list during and after the move (#267).

Auxiliary plus business state, not itself a SharePoint list.
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin, utcnow

# Days and hours: four decimals, handed to Python as float not Decimal, matching
# the request tables so a balance compares equal across the two backends. Four
# decimals because SharePoint balances carry up to 4 (hours / 8, e.g. 0.1875), so
# two would round them on import (#335 fix).
_NUM = Numeric(12, 4, asdecimal=False)


class Location(Base):
    """One office location, the ``Location`` choice list as its own lookup table."""

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # own key
    # The choice value exactly as SharePoint stores it; unique so an import finds
    # the same row again (idempotent) and never files a location twice.
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)


class Department(Base):
    """One department, the ``Department`` choice list as its own lookup table."""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # own key
    # The choice value as SharePoint stores it; unique for idempotent import.
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)


class Staff(Base, TimestampMixin):
    """One Staff Directory person, typed, keyed on the SharePoint item id."""

    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # internal key
    # SharePoint item id -> the stable identity requests and links point at; unique
    # so an import updates the same person instead of duplicating them.
    sp_item_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    # The real SharePoint item id, filled by the write-back once it creates the
    # SharePoint item for a Postgres-native person (#269). Null for an imported
    # row, whose sp_item_id already is the SharePoint id. Both the write-back's
    # later patches and the pull resolve a person through this id too, so the
    # SharePoint item a create makes reconciles to this row instead of a duplicate.
    sp_remote_item_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    # --- identity and contact (Title / EmailAddress / CellNumber / Extension) ---
    name: Mapped[str | None] = mapped_column(String, nullable=True)                 # Title
    # EmailAddress, stored normalised (trimmed, lower case); indexed and unique so
    # the by-email match (#265) is a direct hit. Unique allows many NULLs on both
    # SQLite and Postgres, so people without an email never collide; a blank email
    # is stored as NULL rather than "" to keep that true.
    email: Mapped[str | None] = mapped_column(String, nullable=True, index=True, unique=True)
    cell_number: Mapped[str | None] = mapped_column(String, nullable=True)          # CellNumber
    extension: Mapped[int | None] = mapped_column(Integer, nullable=True)           # Extension (number column)
    birthday: Mapped[date | None] = mapped_column(Date, nullable=True)              # Birthday (date part only)

    # --- placement (Location / Department choice lists, as lookups) ---
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    location: Mapped[Location | None] = relationship(lazy="selectin")               # resolved for the read shape
    department: Mapped[Department | None] = relationship(lazy="selectin")

    # --- employment and the typed supervisor name (SalaryHourly / Supervisor) ---
    salary_hourly: Mapped[str | None] = mapped_column(String, nullable=True)        # SalaryHourly
    supervisor_text: Mapped[str | None] = mapped_column(String, nullable=True)      # Supervisor (free text)
    system_check: Mapped[str | None] = mapped_column(String, nullable=True)         # SystemCheck choice

    # --- grants and the five balance pots (all in days) ---
    vacation_entitlement: Mapped[float | None] = mapped_column(_NUM, nullable=True)  # DefaultYearlyVacationDays
    sick_entitlement: Mapped[float | None] = mapped_column(_NUM, nullable=True)      # SickDayEntitlement
    vacation_balance: Mapped[float | None] = mapped_column(_NUM, nullable=True)      # CurrentVacationBalance
    sick_balance: Mapped[float | None] = mapped_column(_NUM, nullable=True)          # CurrentSickDayBalance
    overtime_balance: Mapped[float | None] = mapped_column(_NUM, nullable=True)      # CurrentOvertimeBalance
    carry_over: Mapped[float | None] = mapped_column(_NUM, nullable=True)            # CarryOver
    payout: Mapped[float | None] = mapped_column(_NUM, nullable=True)                # Payout

    # --- derived and free-text (RequestAllowDate / Comments) ---
    request_allow_date: Mapped[date | None] = mapped_column(Date, nullable=True)    # RequestAllowDate (day only)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)               # Comments

    # --- the person's own Microsoft 365 link (TitleLink), kept so the read shape
    # can rebuild TitleLinkLookupId; the name is kept for a later display. ---
    title_link_sp_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title_link_name: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- SharePoint's own timestamps, kept so the pull (#268) can tell whose edit
    # is newer by modified time, and so the read shape carries Modified/Created. ---
    sp_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sp_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The manager and supervisor links; deleted with the person.
    supervisors: Mapped[list["StaffSupervisor"]] = relationship(
        back_populates="staff",
        cascade="all, delete-orphan",
        foreign_keys="StaffSupervisor.staff_id",
        lazy="selectin",
    )


class StaffSupervisor(Base):
    """One manager or supervisor link from a staff row to another person.

    SharePoint holds the approving managers in the multi-value ``AllManagers``
    person column and one ``SupervisorLink``. Each entry points at a Microsoft
    365 user (its LookupId) and shows a name (its LookupValue). Both are kept
    verbatim so the read shape rebuilds the exact SharePoint person-column blob,
    and ``manager_staff_id`` additionally links to the manager's own staff row
    when that person is themselves in the directory (the owner's ruling that
    supervisors and managers are links between staff rows).
    """

    __tablename__ = "staff_supervisors"
    __table_args__ = (
        # Reads always fetch one person's links; index the owning side.
        Index("ix_staff_supervisors_staff_id", "staff_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # link row id, ordering
    # The person the link belongs to; the FK is the owning staff row.
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    # Which SharePoint column the link came from, so the read shape puts it back
    # where it belongs: "manager" for an AllManagers entry, "supervisor_link" for
    # the single SupervisorLink.
    relation: Mapped[str] = mapped_column(String, nullable=False)
    # The Microsoft 365 user id SharePoint stored (the person-column LookupId),
    # kept exactly so AllManagers / SupervisorLink rebuild unchanged.
    manager_sp_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The display name SharePoint showed (the LookupValue); the approval path and
    # the role checks match a manager by this name today.
    manager_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # The manager's own staff row when they are in the directory; null when the
    # linked Microsoft 365 user is not a staff member (an external approver).
    manager_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)

    staff: Mapped[Staff] = relationship(back_populates="supervisors", foreign_keys=[staff_id])


class StaffWriteback(Base):
    """One pending write of an app-made staff change back to the SharePoint list.

    No longer written: staff write-back was removed (2026-10-18 ruling; Postgres
    is the only copy of staff). The table is kept so no schema change is needed;
    the rest of this docstring describes what it used to hold.

    Every change the app makes to a person's record (a balance movement on
    approval, an HR edit once that path routes through the store) is queued here
    and drained to SharePoint in the background, retried until it lands, so the
    Bulletin Board views, the Microsoft Form and other apps that still read the
    list stay current without ever blocking the user (#267). A pulled edit
    (#268) is never queued, so a change read from SharePoint is not echoed back.
    """

    __tablename__ = "staff_writeback"
    __table_args__ = (
        # The drain picks the next due row; index the two columns it orders by.
        Index("ix_staff_writeback_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # queue row id
    # The SharePoint item the patch is written to; a string like every other id.
    sp_item_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # The SharePoint-shaped patch (column name -> value) to write onto the item.
    fields: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Whether the replay creates the SharePoint item or patches it. A Postgres-
    # native person has no SharePoint item yet, so its first replay is a "create"
    # whose returned id is stored back on the staff row (sp_remote_item_id); every
    # later replay is an "update" patch. Imported rows only ever use "update".
    op: Mapped[str] = mapped_column(String, nullable=False, default="update")
    # pending -> not yet tried, sending -> in flight, done -> written, failed ->
    # last try errored (still retried), dropped -> skipped on purpose.
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)        # tries so far
    # When the row is next eligible; now for a fresh row, later after a failure
    # (backoff). Ordered with status so the drain reads only due rows.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=utcnow
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)              # why the last try failed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # when it landed
