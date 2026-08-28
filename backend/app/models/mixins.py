from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.time_utils import utcnow_aware


class TimestampMixin:
    """Adds created_at / updated_at to a model.

    Shared by the migrated business tables so every row records when it was
    first written (backfilled or created) and when it was last touched (a
    webhook sync or an approval).
    """

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow_aware)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow_aware, onupdate=utcnow_aware
    )
