from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class AdminPassword(Base):
    """The one password that opens the admin dashboard.

    One row. HR changes it from the admin dashboard, which stores a salted
    hash. A developer can also write any plain value straight into
    ``password``; it works as typed and is replaced by its hash at the next
    sign-in. With no row, nobody can sign in: there is no built-in fallback.

    Auxiliary state, like processing_log and email_codes.
    """

    __tablename__ = "admin_password"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # A "pbkdf2_sha256$..." hash, or a plain value a developer typed in.
    password: Mapped[str] = mapped_column(String, nullable=False)
    # When it last changed (UTC), from the dashboard or a sign-in rehash.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
