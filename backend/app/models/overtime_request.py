from datetime import date

from sqlalchemy import Integer, String, Float, Date
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin


class OvertimeRequest(Base, TimestampMixin):
    """An Overtime Requests list item, ported into Postgres.

    Overtime is entered in ``hours`` (the balance engine divides by 8 to get
    days when it credits the Make-Up pot). ``title`` is the free-text
    description and ``date`` is the day worked (SharePoint's StartDate). Same
    ``sp_item_id`` continuity as the other requests so approval links and
    request_approval_state stay valid through the migration.
    """

    __tablename__ = "overtime_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sp_item_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)

    title: Mapped[str | None] = mapped_column(String, nullable=True)      # Title (description)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)        # StartDate (day worked)
    hours: Mapped[float | None] = mapped_column(Float, nullable=True)     # Hours
    status: Mapped[str | None] = mapped_column(String, nullable=True)     # Status

    # Requester (SubmittedBy person field) and assigned manager (ManagerLookupId).
    submitter_sp_user_lookup_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    submitter_name: Mapped[str | None] = mapped_column(String, nullable=True)
    manager_sp_user_lookup_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
