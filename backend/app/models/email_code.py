from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class EmailCode(Base):
    """One emailed sign-in code for the public request page.

    A person proves they own an email address by typing back the 6-digit code
    sent to it. One row per code sent. Only the newest row for an address can
    be used, so asking for a new code quietly retires the older one.

    The code itself is never stored: only an HMAC of it, keyed with the app's
    signing secret, so a database read does not hand out working codes.

    Auxiliary state, like processing_log and dashboard_link_state.
    """

    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Lowercased, trimmed address the code was sent to. Indexed: every check
    # and every rate-limit count is "rows for this email".
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # HMAC-SHA256 of "email:code" with APPROVAL_LINK_SECRET, hex encoded.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Caller IP when the code was requested; counted for the per-IP limit.
    requester_ip: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # When the code was sent (UTC); the expiry and the rate windows run from here.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    # Wrong guesses so far; the code stops working at MAX_ATTEMPTS.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Set when the right code is entered, so a code works only once.
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
