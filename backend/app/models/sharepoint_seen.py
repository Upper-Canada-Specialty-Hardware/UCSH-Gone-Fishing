"""What the post-switch SharePoint sweep decided about each item it saw (#336).

Once requests live in Postgres, SharePoint keeps receiving Microsoft Form items
and may still be edited by hand. A sweep reads each list item and records what
it did with that exact version here, so a later sweep that sees the same
version again does nothing. The ``(kind, sp_item_id, sp_version)`` key is
unique, so each version is acted on once.
"""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import request_vocab as vocab
from app.models.mixins import utcnow


class SharepointSeen(Base):
    """No longer written: the post-switch SharePoint check was removed (2026-10-18
    ruling). The table is kept so no schema change is needed; the rest of this
    docstring describes what it used to hold.

    One SharePoint item version, and what the sweep did with it."""

    __tablename__ = "sharepoint_seen"
    __table_args__ = (
        CheckConstraint(vocab.in_clause("outcome", vocab.SEEN_OUTCOMES), name="ck_sharepoint_seen_outcome"),
        # Each item version is acted on at most once.
        UniqueConstraint("kind", "sp_item_id", "sp_version", name="uq_sharepoint_seen_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)            # which request list it was on
    sp_item_id: Mapped[str] = mapped_column(String, nullable=False)      # the SharePoint item id
    # The version seen: Graph eTag, or lastModifiedDateTime when there is no eTag.
    sp_version: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)         # imported / flagged_* / unchanged
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # the sync_runs row this belongs to
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
