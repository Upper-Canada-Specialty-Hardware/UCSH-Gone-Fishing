from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class DashboardLinkState(Base):
    """When each person was last sent a dashboard link, and when it dies.

    Dashboard links are minted only as a side effect of sending someone an
    email, and they last 30 days. Nothing else records that a link was issued,
    so until now the expiry existed only inside a URL sitting in an inbox — the
    system had no way to know whose access was about to lapse, and therefore no
    way to act on it. This table is that record.

    One row per person, keyed on the Staff Directory item id, because access is
    a property of a person rather than of a request. That is the difference from
    ``RequestApprovalState``, which tracks emails per request and is why manager
    links stay fresh while employee links quietly expire.

    ``expires_at`` is what the renewal task reads, not ``last_sent_at``. The two
    are the same thing today — expiry is always the send date plus thirty days —
    but the question being asked is "is this person about to lose access", and
    only the expiry stays truthful if that window is ever changed.
    """

    __tablename__ = "dashboard_link_state"

    # Staff Directory item id — the same value used as the dashboard link's uid.
    employee_id: Mapped[str] = mapped_column(String, primary_key=True)

    # When a link was last emailed to this person, for diagnosis. Not the field
    # renewal decides on.
    last_sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    # When the most recently issued link stops validating. Indexed because the
    # renewal task's only query is a range scan over it.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
