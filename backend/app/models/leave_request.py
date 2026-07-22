from datetime import date

from sqlalchemy import Integer, String, Float, Date
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin


class LeaveRequest(Base, TimestampMixin):
    """A Leave Requests list item, ported into Postgres.

    Covers the whole request lifecycle the dashboard and approval flow read:
    the type, the pending/processed status, the date range, the computed
    business ``days``, and the resolved requester + manager. SharePoint still
    receives the item on intake (form or webhook), so ``sp_item_id`` keeps the
    request tied to its SharePoint id — the same key that approval links and
    request_approval_state already key on.

    A pending item is only "fully processed" once it has both ``days`` and a
    ``manager_sp_user_lookup_id`` (the dispatcher fills these in for
    SP-created items); dashboards hide it until then.
    """

    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sp_item_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)

    leave_type: Mapped[str | None] = mapped_column(String, nullable=True)             # LeaveType
    status: Mapped[str | None] = mapped_column(String, nullable=True)                 # Status
    approve_processed_flag: Mapped[str | None] = mapped_column(String, nullable=True)  # ApproveProcessedFlag

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)              # StartDate
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)                # EndDate
    days: Mapped[float | None] = mapped_column(Float, nullable=True)                  # Days (computed)
    partial_hours: Mapped[float | None] = mapped_column(Float, nullable=True)         # PartialHours (half/partial day)

    title: Mapped[str | None] = mapped_column(String, nullable=True)                  # Title
    notes: Mapped[str | None] = mapped_column(String, nullable=True)                  # Notes

    # Requester (SubmittedTest person field) and assigned manager (ManagerLookupId).
    submitter_sp_user_lookup_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    submitter_name: Mapped[str | None] = mapped_column(String, nullable=True)
    manager_sp_user_lookup_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Denormalized org placement stamped onto the request at assignment time.
    staff_location: Mapped[str | None] = mapped_column(String, nullable=True)         # StaffLocation
    staff_department: Mapped[str | None] = mapped_column(String, nullable=True)       # StaffDepartment
