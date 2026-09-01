from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class StaffSetupNudge(Base):
    """What the creator of a broken Staff Directory record has been told, and when.

    The setup sweep can name every record that would stall a request, but
    knowing is not telling: until this table existed the list sat on a dashboard
    tab nobody opens until someone is already blocked. The daily nudge emails
    the record's creator instead, and this row is the only thing standing
    between "we told them" and "we tell them again every morning".

    One row per Staff Directory record, keyed on its item id, because the record
    is what is broken - the same person may have created several, each with its
    own history.

    ``issue_signature`` is the sorted fail codes joined by commas. It is what
    makes a changed problem a new conversation: the weekly cadence only applies
    while the same set of things is wrong, and a record that breaks in a new way
    is emailed straight away rather than waiting out the week.

    A row is DELETED once its record stops being flagged, so a later breakage
    starts a fresh first email rather than resuming a cadence from months ago.
    That is why ``send_count`` counts one uninterrupted run of nudges and not
    the record's lifetime total.
    """

    __tablename__ = "staff_setup_nudge"

    # Staff Directory item id of the record whose setup is broken.
    employee_id: Mapped[str] = mapped_column(String, primary_key=True)

    # Sorted fail codes joined by commas - the problem set as last emailed.
    issue_signature: Mapped[str] = mapped_column(String, nullable=False)

    # Who was emailed: the record's creator, never anybody else.
    recipient: Mapped[str] = mapped_column(String, nullable=False)

    # When this run of nudges started. Preserved across re-nudges, so it shows
    # how long the record has been broken.
    first_sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    # When the last nudge went out. The field the weekly cadence is measured
    # from.
    last_sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    # How many nudges this run has sent, including the first.
    send_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
