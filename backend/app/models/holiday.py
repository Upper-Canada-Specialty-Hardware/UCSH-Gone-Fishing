from datetime import date

from sqlalchemy import Integer, String, Date
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin


class Holiday(Base, TimestampMixin):
    """A Company Holidays list item, ported into Postgres.

    Two kinds of rows share this list, distinguished by ``title``:
      - a stat holiday for a ``province`` on a ``date``, and
      - the half-Friday season markers ("Half Fridays START" / "... END"),
        whose ``date`` bounds the summer half-day-Friday window.
    The business-day calculator filters these by province the same way it does
    against SharePoint today.
    """

    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sp_item_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)

    title: Mapped[str | None] = mapped_column(String, nullable=True)              # Title (holiday name / season marker)
    date: Mapped[date | None] = mapped_column(Date, index=True, nullable=True)    # Date
    province: Mapped[str | None] = mapped_column(String, index=True, nullable=True)  # Province
