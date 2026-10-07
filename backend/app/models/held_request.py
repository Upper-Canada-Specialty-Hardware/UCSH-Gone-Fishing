from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class HeldRequest(Base):
    """A request from someone not in the Staff Directory yet, waiting for them.

    The public request page lets anyone who proves they own an email address
    fill in a form. When that address is not on any staff record, nothing can
    route the request (no balances, no manager), so it is kept here and the
    supervisor they picked is emailed to add them. Adding them releases it:
    the stored form is submitted as a normal request (services/held_requests.py).

    Auxiliary state, like email_codes. Not a SharePoint list.
    """

    __tablename__ = "held_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Verified, lowercased address; how adding the employee finds their requests.
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Name and location as the person typed them; prefill Add Employee.
    name: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False)

    # The supervisor they picked: Staff Directory item id, plus the name and
    # email at the time, so the reminders do not depend on a later lookup.
    supervisor_id: Mapped[str] = mapped_column(String, nullable=False)
    supervisor_name: Mapped[str] = mapped_column(String, nullable=False)
    supervisor_email: Mapped[str] = mapped_column(String, nullable=False)

    # "leave", "overtime" or "carryover-payout", and the checked form as JSON.
    request_type: Mapped[str] = mapped_column(String, nullable=False)
    form_data: Mapped[dict] = mapped_column(JSON, nullable=False)

    # "held" (waiting), "released" (submitted as a request), "failed" (release
    # tried and SharePoint refused; retried by an admin), "cancelled".
    status: Mapped[str] = mapped_column(String, nullable=False, default="held", index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    # Reminder trail: the supervisor once more after 2 business days, then the
    # admins after 5. Null until sent, so each is sent once.
    supervisor_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admins_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Release outcome.
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sp_item_id: Mapped[str | None] = mapped_column(String, nullable=True)   # the created request
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)     # why a release failed
