from datetime import datetime

from sqlalchemy import Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CarryoverResetLog(Base):
    __tablename__ = "carryover_reset_log"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
