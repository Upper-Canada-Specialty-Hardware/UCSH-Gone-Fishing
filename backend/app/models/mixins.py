from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    datetime.utcnow() is deprecated in Python 3.12+ (it returns a naive
    datetime); datetime.now(timezone.utc) is the tz-aware replacement. Paired
    with DateTime(timezone=True) columns below so the aware value round-trips
    as timestamptz on Postgres (asyncpg rejects aware values in a naive column).
    """
    return datetime.now(timezone.utc)


class TimestampMixin:
    """Adds created_at / updated_at to a model.

    Shared by the migrated business tables so every row records when it was
    first written (backfilled or created) and when it was last touched (a
    webhook sync or an approval).
    """

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
