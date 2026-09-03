from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import utcnow


class EmailApiLog(Base):
    """One row per HTTP call the backend makes to SMTP2GO's send endpoint.

    This is the record of the exchange itself: what the backend asked SMTP2GO
    to send, and exactly what SMTP2GO answered, kept verbatim. It settles one
    question when someone reports "I never got the email":

    * a row with HTTP 200 whose body says the recipient was accepted, and no
      email in the inbox, means the problem is past our code (SMTP2GO or the
      mailbox side);
    * a row whose body reports the recipient as failed means our request or
      our data was wrong;
    * no row at all means the code never attempted the send.

    The request and response columns are the evidence. The derived columns
    (outcome, counts, ids) exist only so the admin page can filter and display
    without parsing JSON.

    Two things are never stored: the API key, and the HTML body. Bodies carry
    signed approval and dashboard links, and this table is read by an
    unauthenticated admin endpoint, so the body is represented by its byte
    length and a SHA-256 only.
    """

    __tablename__ = "email_api_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # When the request was made (UTC). Indexed: every lookup is newest-first
    # inside a date window.
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    # Round trip in milliseconds; null when SMTP2GO was never called.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- the request we made ---
    request_url: Mapped[str] = mapped_column(String, nullable=False)
    sender: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON of the payload with api_key removed and html_body replaced by its
    # byte length and SHA-256. Recipients appear exactly as they were sent.
    request_json: Mapped[str] = mapped_column(Text, nullable=False)

    # --- the answer we got ---
    # HTTP status; null when SMTP2GO never answered (timeout, DNS, refused)
    # or was never called (no usable recipient).
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Response body exactly as received, capped at RESPONSE_MAX_CHARS.
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why there is no HTTP answer: the exception text, or the reason the call
    # was never made. Null whenever SMTP2GO answered.
    no_response_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- derived from the answer, for filtering and display ---
    # One of the OUTCOME_* constants in services/email_api_log.py.
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    # data.succeeded / data.failed from the response body, when readable.
    succeeded_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # SMTP2GO's identifiers: what their activity dashboard and their support
    # search by once the question moves to "did they deliver it".
    smtp2go_email_id: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp2go_request_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Loaded with the row (selectin) so the admin page gets To/CC in one query.
    recipients: Mapped[list["EmailApiLogRecipient"]] = relationship(
        back_populates="log", cascade="all, delete-orphan", lazy="selectin"
    )


class EmailApiLogRecipient(Base):
    """One address on one SMTP2GO call, so a person's emails are an exact join.

    Addresses are stored lower-cased and stripped. The Staff Directory holds
    real values with trailing whitespace and mixed case, and the lookup must
    still find them.
    """

    __tablename__ = "email_api_log_recipient"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Deleting a log row takes its recipient rows with it.
    log_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("email_api_log.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Normalised address; indexed because the admin lookup is by address.
    address: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # "to" or "cc": which field of the request carried this address.
    field: Mapped[str] = mapped_column(String(4), nullable=False)

    log: Mapped[EmailApiLog] = relationship(back_populates="recipients")
