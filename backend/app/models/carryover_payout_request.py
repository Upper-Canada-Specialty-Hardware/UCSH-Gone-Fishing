from sqlalchemy import Integer, String, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin


class CarryoverPayoutRequest(Base, TimestampMixin):
    """A Carryover / Payout list item, ported into Postgres.

    One list backs both movements out of the Vacation pot: ``type_of_request``
    is either "Carry Over" (days become use-it-or-lose-it carryover) or
    "Payout" (days are cashed out by payroll). ``system_state`` is this list's
    processing flag (SharePoint's SystemState, e.g. "Not Processed").

    ``employee_sp_item_id`` mirrors SharePoint's EmployeeID column, which points
    at the Staff Directory item id (used to resolve the submitter's name on the
    dashboard); the requester person field is captured separately in
    ``submitter_sp_user_lookup_id``.
    """

    __tablename__ = "carryover_payout_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sp_item_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)

    type_of_request: Mapped[str | None] = mapped_column(String, nullable=True)   # TypeofRequest
    days: Mapped[float | None] = mapped_column(Float, nullable=True)             # Days
    system_state: Mapped[str | None] = mapped_column(String, nullable=True)      # SystemState

    submitter_sp_user_lookup_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    employee_sp_item_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)  # EmployeeID
    manager_sp_user_lookup_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
