"""The one typed table all three request kinds live in (#321).

``RequestRecord`` replaces the JSON-blob request tables (0013) with real
columns, so the database can index, constrain and query a request instead of
treating it as opaque. One row is one leave, overtime or carry-over/payout
request, told apart by ``kind``. ``item_id`` is the number people and links use
(``#3905``); ``id`` is an internal surrogate that foreign keys point at and
nobody sees. CHECK constraints enforce the per-kind required fields and the
closed value sets from ``request_vocab``, so a malformed request cannot be
stored.

The SharePoint-shaped adapter (repositories/postgres/typed_requests.py) reads
and writes this table through one column map and keeps any column it does not
recognise in ``extra``, so nothing SharePoint carried is lost in the move.
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import request_vocab as vocab
from app.models.mixins import utcnow


class RequestRecord(Base):
    """One leave, overtime or carry-over/payout request, fully typed."""

    __tablename__ = "requests"
    __table_args__ = (
        # One item number per kind, and one row per source SharePoint item.
        UniqueConstraint("kind", "item_id", name="uq_requests_kind_item_id"),
        UniqueConstraint("kind", "sp_item_id", name="uq_requests_kind_sp_item_id"),
        # Closed value sets, mirrored from request_vocab so code and table agree.
        CheckConstraint(vocab.in_clause("kind", vocab.KINDS), name="ck_requests_kind"),
        CheckConstraint(vocab.in_clause("status", vocab.STATUSES), name="ck_requests_status"),
        CheckConstraint(vocab.in_clause("state", vocab.STATES), name="ck_requests_state"),
        CheckConstraint(
            vocab.in_clause("balance_state", vocab.BALANCE_STATES), name="ck_requests_balance_state"
        ),
        CheckConstraint(
            f"leave_type IS NULL OR {vocab.in_clause('leave_type', vocab.LEAVE_TYPES_ALLOWED)}",
            name="ck_requests_leave_type",
        ),
        CheckConstraint(
            f"employment_type IS NULL OR {vocab.in_clause('employment_type', vocab.EMPLOYMENT_TYPES)}",
            name="ck_requests_employment_type",
        ),
        CheckConstraint(
            f"request_option IS NULL OR {vocab.in_clause('request_option', vocab.REQUEST_OPTIONS)}",
            name="ck_requests_request_option",
        ),
        # Per-kind required fields: enforced only for the kind they belong to.
        CheckConstraint(
            "kind != 'leave' OR (leave_type IS NOT NULL AND start_date IS NOT NULL)",
            name="ck_requests_leave_fields",
        ),
        CheckConstraint(
            "kind != 'overtime' OR (start_date IS NOT NULL AND hours > 0)",
            name="ck_requests_overtime_fields",
        ),
        CheckConstraint(
            "kind != 'carryover_payout' OR (request_option IS NOT NULL AND days > 0)",
            name="ck_requests_carryover_fields",
        ),
        # A range cannot end before it starts, and amounts are never negative.
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="ck_requests_date_order",
        ),
        CheckConstraint("days IS NULL OR days >= 0", name="ck_requests_days_nonneg"),
        CheckConstraint("hours IS NULL OR hours >= 0", name="ck_requests_hours_nonneg"),
        CheckConstraint(
            "partial_hours IS NULL OR partial_hours >= 0", name="ck_requests_partial_hours_nonneg"
        ),
        # The reads the dashboards and streams make, indexed to stay cheap.
        Index("ix_requests_employee_created", "employee_id", "created_at"),
        Index("ix_requests_manager_status", "manager_id", "status"),
        Index("ix_requests_status_created", "status", "created_at"),
        Index("ix_requests_kind_start_date", "kind", "start_date"),
    )

    # --- identity and status ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)   # internal surrogate key
    kind: Mapped[str] = mapped_column(String, nullable=False)                        # leave / overtime / carryover_payout
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)                    # the number people and links use
    sp_item_id: Mapped[str | None] = mapped_column(String, nullable=True)            # source SharePoint item, if imported
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")   # lifecycle status, stored lower case
    # The explicit state-machine position (#455): the backend owns it, the frontend
    # renders it. Defaults to getting_ready so a brand-new row is "being prepared"
    # until submit (or a decision) moves it on. See services/request_states.py.
    state: Mapped[str] = mapped_column(String, nullable=False, default="getting_ready")
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)                   # latest stage in words, kept in step with events

    # --- people ---
    employee_id: Mapped[str] = mapped_column(String, nullable=False, index=True)     # Staff Directory id of who it is for
    employee_name: Mapped[str | None] = mapped_column(String, nullable=True)         # their name at the time
    employee_email: Mapped[str | None] = mapped_column(String, nullable=True)        # lower case address
    manager_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # the deciding manager's id
    manager_name: Mapped[str | None] = mapped_column(String, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String, nullable=True)       # Salary / Hourly / Hourly BC, null for legacy
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    department: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- request details ---
    submitted_channel: Mapped[str | None] = mapped_column(String, nullable=True)     # where the request came in from
    leave_type: Mapped[str | None] = mapped_column(String, nullable=True)            # leave only
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)             # first day, or the date worked (overtime)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)               # last day, leave range only
    # Numeric(12, 4): SharePoint balances carry up to 4 decimals (hours / 8, e.g.
    # 0.1875), so two decimals would round them on import (#335 fix).
    partial_hours: Mapped[float | None] = mapped_column(Numeric(12, 4, asdecimal=False), nullable=True)  # part-day leave
    days: Mapped[float | None] = mapped_column(Numeric(12, 4, asdecimal=False), nullable=True)           # cost in days
    hours: Mapped[float | None] = mapped_column(Numeric(12, 4, asdecimal=False), nullable=True)          # overtime hours
    request_option: Mapped[str | None] = mapped_column(String, nullable=True)        # carry_over / payout
    description: Mapped[str | None] = mapped_column(Text, nullable=True)             # employee-entered text
    bereavement_relation: Mapped[str | None] = mapped_column(String, nullable=True)  # relation, bereavement leave
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- decision and balance ---
    approval_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # bumps invalidate old links
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_channel: Mapped[str | None] = mapped_column(String, nullable=True)
    balance_state: Mapped[str] = mapped_column(String, nullable=False, default="not_applied")  # how far the pots moved

    # --- legacy, reminders and bookkeeping ---
    legacy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)      # decided before 2026-03-23
    reminder_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # SharePoint-only leftovers kept verbatim; new code only writes this through the adapter.
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
