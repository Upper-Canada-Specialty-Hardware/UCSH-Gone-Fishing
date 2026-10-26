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
    # Name and department as the person gave them; prefill Add Employee.
    name: Mapped[str] = mapped_column(String, nullable=False)
    department: Mapped[str | None] = mapped_column(String, nullable=True)
    # Rows held before #226 picked a location instead of a department.
    location: Mapped[str | None] = mapped_column(String, nullable=True)

    # The supervisor they picked: Staff Directory item id, plus the name and
    # email at the time, so the reminders do not depend on a later lookup.
    supervisor_id: Mapped[str] = mapped_column(String, nullable=False)
    supervisor_name: Mapped[str] = mapped_column(String, nullable=False)
    supervisor_email: Mapped[str] = mapped_column(String, nullable=False)

    # "leave", "overtime" or "carryover-payout", and the checked form as JSON.
    request_type: Mapped[str] = mapped_column(String, nullable=False)
    form_data: Mapped[dict] = mapped_column(JSON, nullable=False)

    # "held" (waiting), "released" (submitted as a request), "failed" (release
    # hit an unexpected SharePoint/DB/network error; retried by an admin),
    # "refused" (#406 - the release reached submit_request and it refused the form
    # on a business rule, e.g. a date clash; closed, never retried, the person is
    # emailed), "not_on_team" (a supervisor said the person is not on their team;
    # waiting for them to pick another), "cancelled".
    status: Mapped[str] = mapped_column(String, nullable=False, default="held", index=True)

    # Every supervisor this request has been sent to, in order, each as
    # {"id", "name", "email", "outcome"}: outcome is null while they hold it,
    # "not_on_team" once they declined it, "handled" once the person was added.
    # Drives the "told it moved" emails and the one thread per request per
    # recipient (#443).
    supervisors_involved: Mapped[list | None] = mapped_column(JSON, nullable=True)

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

    # Cancelling needs a reason and can be undone (#445): the reason an admin
    # gave, and the status to put back if the cancel is reversed.
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_before_cancel: Mapped[str | None] = mapped_column(String, nullable=True)
