from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class EmployeeInvite(Base):
    """The latest site-access attempt for one employee, from Add Employee.

    Adding an employee asks Microsoft 365 (Graph) to make sure the person can
    open the SharePoint site: find them in the tenant or invite them as a
    guest, then add them to the Entra group that sits inside the site's
    Members. This row records how that went, so the admin dashboard can show
    it and offer a resend. One row per employee, overwritten on each attempt.

    Auxiliary state, like held_requests. Not a SharePoint list.
    """

    __tablename__ = "employee_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Staff Directory item id; one row per employee.
    employee_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)

    # "in_tenant" (already a user, added to the group), "invited" (guest invite
    # sent, added to the group), "skipped" (invites are off), "failed".
    status: Mapped[str] = mapped_column(String, nullable=False)
    # Entra object id of the user or invited guest, once known.
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Whether they are now in the site members group.
    group_added: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Readable outcome or error, for the admin dashboard.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How many times this employee has been invited (first send plus resends).
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
