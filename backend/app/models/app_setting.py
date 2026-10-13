from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class AppSetting(Base):
    """One setting an admin changes from the dashboard, by name.

    Settings that must change without a redeploy live here instead of in
    config.py (environment variables). A missing row means the setting's
    default, so a fresh database needs no seeding (services/app_settings.py).

    Auxiliary state, like held_requests. Not a SharePoint list.
    """

    __tablename__ = "app_settings"

    # The setting's name, e.g. "carryover_payout_open".
    key: Mapped[str] = mapped_column(String, primary_key=True)
    # Its value as JSON, so a setting can be a flag, a number or a date range.
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    # When it last changed, for anyone asking "since when is this on?".
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False,
    )
