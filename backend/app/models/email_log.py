from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class EmailLog(Base):
    """One row per outbound email attempt through SMTP2GO.

    Until this table existed the only trace of an email was a log line on
    stdout, which scrolls out of the hosting log window and cannot be searched
    by person. When an employee reports "I never got the email" the system had
    nothing to say about whether it was sent, to which address, or whether
    SMTP2GO accepted it. This table is that record.

    A row is written for every attempt, not only successes: a rejected API
    call, a partial acceptance, a network error, and a send that was skipped
    because the Staff Directory address was blank all leave a row. The skipped
    case matters most for diagnosis, because a blank address raises no error
    anywhere else in the pipeline.

    Nothing reads this table on a hot path. It is an audit log queried by the
    admin email-log endpoint, so a failed write must never turn a delivered
    email into an error for the caller (see ``services/email_log.py``).
    """

    __tablename__ = "email_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # When the attempt finished. Indexed: every lookup is newest-first.
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    # Outcome: "sent" | "partial" | "failed" | "skipped".
    # The constants live in services/email_log.py next to the writer.
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    # Recipients, normalised (lower-case, whitespace stripped) and packed as
    # ",a@x.com,b@x.com," so a LIKE '%,a@x.com,%' match is exact per address
    # and cannot hit "ana@x.com".
    to_addresses: Mapped[str] = mapped_column(Text, nullable=False)
    cc_addresses: Mapped[str | None] = mapped_column(Text, nullable=True)

    subject: Mapped[str] = mapped_column(Text, nullable=False)

    # Staff Directory item id of the person the email was primarily for, when
    # the caller knew it (every dashboard-footer send does). Indexed so the
    # admin lookup by employee id is a direct hit rather than a scan.
    primary_employee_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )

    # SMTP2GO's identifiers for the accepted message. These are what to quote
    # to SMTP2GO support, and what their activity dashboard is searched by,
    # when the question moves from "did we send it" to "did they deliver it".
    smtp2go_email_id: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp2go_request_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # HTTP status SMTP2GO answered with; null when the request never completed
    # (network error, timeout) or was never made (skipped).
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Why a send failed or was skipped, truncated. Null on a clean send.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
