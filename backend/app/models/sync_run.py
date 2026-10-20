"""One run of a sync job, with its counts and any error (#334, #336).

The import, the shadow and final syncs, and the SharePoint sweep each write a
row when they start and update it when they finish, so an operator can see what
each run did (``counts``) and whether it failed (``error``). ``sharepoint_seen``
and ``legacy_findings`` rows point back at the run that produced them.
"""
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import request_vocab as vocab
from app.models.mixins import utcnow


class SyncRun(Base):
    """One run of a sync or sweep job."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint(vocab.in_clause("job", vocab.SYNC_JOBS), name="ck_sync_runs_job"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String, nullable=False)              # import / shadow_sync / final_sync / sharepoint_check
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # what the run did, e.g. {"imported": 12}
    error: Mapped[str | None] = mapped_column(Text, nullable=True)        # why it failed, if it did
