from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import utcnow


class EmailApiLog(Base):
    """One row per HTTP call the backend makes to an email service's send endpoint.

    The service is either SMTP2GO or the UCSH mailer (Microsoft HVE); which one
    is derived from ``request_url`` (see ``service_for_url``). This is the record
    of the exchange itself: what the backend asked the service to send, and
    exactly what it answered, kept verbatim. It settles one question when someone
    reports "I never got the email":

    * a row with HTTP 200 whose body says the recipient was accepted, and no
      email in the inbox, means the problem is past our code (the service's
      delivery or the mailbox side);
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
    # Round trip in milliseconds; null when the service was never called.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- the request we made ---
    request_url: Mapped[str] = mapped_column(String, nullable=False)
    sender: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON of the payload with api_key removed and html_body replaced by its
    # byte length and SHA-256. Recipients appear exactly as they were sent.
    request_json: Mapped[str] = mapped_column(Text, nullable=False)

    # --- the answer we got ---
    # HTTP status; null when the service never answered (timeout, DNS, refused)
    # or was never called (no usable recipient).
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Response body exactly as received, capped at RESPONSE_MAX_CHARS.
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why there is no HTTP answer: the exception text, or the reason the call
    # was never made. Null whenever the service answered.
    no_response_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- derived from the answer, for filtering and display ---
    # One of the OUTCOME_* constants in services/email_api_log.py.
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    # data.succeeded / data.failed from the response body, when readable.
    succeeded_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The service's identifiers, kept under the original column names for
    # compatibility: for SMTP2GO its email_id / request_id (what its activity
    # dashboard and support search by); for the UCSH mailer its message id(s) in
    # smtp2go_email_id. The API exposes smtp2go_email_id as the neutral message_id.
    smtp2go_email_id: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp2go_request_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Loaded with the row (selectin) so the admin page gets To/CC in one query.
    recipients: Mapped[list["EmailApiLogRecipient"]] = relationship(
        back_populates="log", cascade="all, delete-orphan", lazy="selectin"
    )


class EmailApiLogRecipient(Base):
    """One address on one send call, so a person's emails are an exact join.

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
