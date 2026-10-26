"""Record every SMS the system sends through Twilio, and every reply it gets.

Twilio's own console is not available to developers, so these two tables are the
only evidence of what the system texted and what Twilio did with it. They mirror
``email_api_log`` for the SMS channel:

* ``SmsApiLog`` holds one row per Twilio send attempt (a manager approval ask, a
  result text, a reminder, an instant reply to an inbound text, or an admin test)
  whatever happened: a success, an HTTP error, a network failure, a rehearsal
  skip, or a number Twilio could never parse. The send's own answer (the message
  SID and queued status, or the error code) is kept, and the delivery columns are
  filled in later from Twilio's status callbacks (#496), so an "accepted but never
  delivered" case is visible.
* ``SmsReplyLog`` holds one row per inbound text Twilio posts to our webhook: the
  parsed decision, who it matched, and what the handler did with it (#497).

The full E.164 number is stored on purpose (owner ruling): Railway/stdout log
lines stay masked (#420), but this table is the one place a full number lives so
an admin can match a person to their texts. SMS bodies carry no signed links
(unlike email), so the body is stored verbatim. The Twilio Account SID is
scrubbed out of the stored response, and the auth token never reaches here (it
rides in the Authorization header, not the body).
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class SmsApiLog(Base):
    """One row per Twilio send attempt, whatever the outcome.

    The question this settles: did the backend ask Twilio to text this number,
    what did Twilio answer, and did the message actually get delivered. A row
    with a 2xx answer and a later ``delivery_status`` of ``undelivered`` means
    the problem is past our code; no row at all means the code never tried.

    The send columns (``http_status``, ``response_body``, ``twilio_sid`` ...) are
    written once, when the send happens. The delivery columns
    (``delivery_status`` ...) are written later, each time Twilio posts a status
    callback for this message's SID (#496).
    """

    __tablename__ = "sms_api_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # When the send was attempted (UTC). Indexed: every lookup is newest-first
    # inside a day window.
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    # Round trip to Twilio in milliseconds; set even for a skip or a bad number.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # What kind of text this was: the outbox message_type (manager_approval,
    # reminder, result, rejection, receipt, bereavement_alert), "reply" for an
    # instant answer to an inbound text, "test" for an admin test, or "other".
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="other")

    # --- optional request context, when the caller knows it ---
    # The request this text was about (leave / overtime / carryover-payout) and
    # its item id, so the admin page can list every text for one request.
    request_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    request_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The outbox row and request record this came from, where there was one.
    outbox_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # For an instant reply, the inbound reply row (SmsReplyLog.id) that triggered
    # it, so the text we sent back can be tied to the text we received. Null for
    # every outbound send that was not a reply.
    inbound_reply_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # --- who it went to ---
    # The recipient in full E.164 (owner ruling), or the raw value when Twilio
    # could not parse it into a number.
    to_number: Mapped[str] = mapped_column(String, nullable=False)
    # The last ten digits of to_number, the lookup key: an employee's cell is
    # matched on its last ten digits the same way the SMS webhook matches a
    # sender. Indexed because the admin lookup is by number.
    to_digits: Mapped[str] = mapped_column(String(10), nullable=False, default="", index=True)
    # Our Twilio sending number.
    from_number: Mapped[str | None] = mapped_column(String, nullable=True)
    # The message body verbatim (SMS carries no signed links, so this is safe).
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- what Twilio answered to the send ---
    # HTTP status; null when Twilio never answered (network error) or was never
    # called (a rehearsal skip, or a number that would not parse).
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Twilio's JSON verbatim, capped, with the Account SID scrubbed out (#420).
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why there is no HTTP answer: the exception text, the rehearsal skip, or the
    # bad-number reason. Null whenever Twilio answered.
    no_response_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Twilio's message SID (read off the answer); the key status callbacks match
    # on. Indexed for that lookup.
    twilio_sid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Twilio's message status at send time (queued / sent / ...).
    twilio_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Twilio's error code and message, when the send itself was refused.
    error_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- filled in later from Twilio's delivery status callbacks (#496) ---
    # The latest delivery status Twilio reported (sent / delivered / undelivered
    # / failed ...); null until the first callback lands.
    delivery_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # The error code on an undelivered/failed callback (Twilio's numeric code).
    delivery_error_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # When the latest delivery callback was applied (UTC).
    delivery_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The full history of delivery callbacks as a JSON list of
    # ``{"status": ..., "error_code": ..., "at": <iso>}``, so a message that went
    # queued -> sent -> delivered keeps every step, not just the last.
    delivery_history: Mapped[str | None] = mapped_column(Text, nullable=True)


class SmsReplyLog(Base):
    """One row per inbound text Twilio posts to ``/api/twilio/sms`` (#497).

    This is the record of what we received and what the webhook handler decided:
    whether the Twilio signature was valid, what decision/request the text parsed
    to, which staff member the sender matched, and an ``outcome`` naming the exact
    branch the handler took (a bad signature, processing off, an unrecognized
    body, an approval, a refusal, and so on). The outbound text the handler sent
    back is a ``SmsApiLog`` row of kind ``reply`` whose ``inbound_reply_id``
    points at this row.
    """

    __tablename__ = "sms_reply_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # When the inbound text was received (UTC). Indexed for newest-first lookups.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    # Twilio's SID for the inbound message, when it sent one.
    message_sid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Who texted in (full), and the last ten digits as the lookup key.
    from_number: Mapped[str | None] = mapped_column(String, nullable=True)
    from_digits: Mapped[str] = mapped_column(String(10), nullable=False, default="", index=True)
    # The Twilio number the text came in on.
    to_number: Mapped[str | None] = mapped_column(String, nullable=True)
    # The inbound body verbatim.
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Whether the X-Twilio-Signature validated; a false here is an outcome of its
    # own (a forged or misconfigured inbound).
    signature_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- what the body parsed to (null when it did not parse) ---
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)   # Approve / Reject
    request_type: Mapped[str | None] = mapped_column(String(32), nullable=True)  # leave / overtime / carryover-payout
    item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)           # a reject's free-text reason

    # The Staff Directory id of the sender, when their number matched someone.
    sender_staff_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Which branch the handler took: one of the OUTCOME_* constants in
    # services/sms_api_log.py.
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, default="error")
    # The handler's error text for a refusal, or the exception text for an error.
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
