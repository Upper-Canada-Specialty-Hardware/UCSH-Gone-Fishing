from datetime import date

from sqlalchemy import Integer, String, Float, Date
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin


class Employee(Base, TimestampMixin):
    """A Staff Directory record, ported into Postgres.

    Holds the five balance "pots" (all in days), the entitlements, and the
    identity link back to Microsoft 365. SharePoint keeps ONLY identity after
    the migration, so the two identity columns are the bridge:
      - ``email``            — the @ucsh work email (the durable identity key)
      - ``sp_user_lookup_id`` — the User Information List lookup id that Graph
        Person/Group fields point at, so a request's SubmittedBy/Manager
        lookup id can be resolved straight to an employee row instead of the
        current name round-trip.

    ``sp_item_id`` is the Staff Directory list item id — the value the code
    uses today as the "employee id" (get_employee_by_id) and the natural key
    for idempotent upsert during backfill and webhook sync.

    Column names are snake_case (new-code convention). The repository layer
    translates to/from the SharePoint field shape the balance engine's pure
    functions expect (CurrentVacationBalance, CarryOver, ...), so those pure
    functions stay untouched.
    """

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Staff Directory list item id (today's "employee id").
    sp_item_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)

    # --- Identity (the only thing SharePoint keeps) ---
    email: Mapped[str | None] = mapped_column(String, index=True, nullable=True)        # EmailAddress
    sp_user_lookup_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)                            # Title

    # --- Org placement ---
    department: Mapped[str | None] = mapped_column(String, nullable=True)                # Department
    location: Mapped[str | None] = mapped_column(String, nullable=True)                  # Location
    employee_type: Mapped[str | None] = mapped_column(String, nullable=True)             # EmployeeType

    # --- The five balance "pots" (days) ---
    vacation_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # CurrentVacationBalance
    sick_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)      # CurrentSickDayBalance
    overtime_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # CurrentOvertimeBalance (Make-Up)
    carryover_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # CarryOver
    payout_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)    # Payout

    # --- Entitlements / allotments ---
    vacation_entitlement: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # DefaultYearlyVacationDays
    sick_entitlement: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)      # SickDayEntitlement

    # --- Eligibility gate ---
    request_allow_date: Mapped[date | None] = mapped_column(Date, nullable=True)         # RequestAllowDate
