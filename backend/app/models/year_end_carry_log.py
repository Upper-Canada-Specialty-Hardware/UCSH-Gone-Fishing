"""One row per person carried at a year end (#553), so the carry runs once each."""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class YearEndCarryLog(Base):
    """A person whose unused vacation was carried for one ended year."""

    __tablename__ = "year_end_carry_log"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)              # the year that ended
    employee_id: Mapped[str] = mapped_column(String(32), primary_key=True)    # their staff id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
