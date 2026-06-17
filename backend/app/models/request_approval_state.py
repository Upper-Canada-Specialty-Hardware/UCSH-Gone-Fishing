from datetime import datetime

from sqlalchemy import Integer, String, DateTime, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RequestApprovalState(Base):
    __tablename__ = "request_approval_state"

    list_id: Mapped[str] = mapped_column(String, primary_key=True)
    item_id: Mapped[str] = mapped_column(String, primary_key=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    previous_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_emailed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    # Reminder follow-up tracking. reminder_count = how many reminder re-sends have
    # gone out (0 = only the original/edit emails). reminders_closed = stop reminding
    # (request actioned or past its cutoff date).
    reminder_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reminders_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
