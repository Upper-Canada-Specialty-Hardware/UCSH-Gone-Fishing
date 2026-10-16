"""Something the migration or a sweep found wrong, for HR to resolve (#337, #341).

Importing years of SharePoint data turns up requests and staff records that do
not add up: an approved request that never moved a balance, a suspect
employment type, a date error, a SharePoint change made after the switch. Each
is recorded here with its evidence and a plain reason, and HR marks it resolved
or ignored. The ``fingerprint`` (``case:subject:kind:subject_id``) is unique,
so a rerun of the same check never files a duplicate.
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import request_vocab as vocab
from app.models.mixins import utcnow


class LegacyFinding(Base):
    """One reconciliation finding awaiting an HR decision."""

    __tablename__ = "legacy_findings"
    __table_args__ = (
        CheckConstraint(vocab.in_clause("subject", vocab.FINDING_SUBJECTS), name="ck_legacy_findings_subject"),
        # "case" is quoted: it is a SQL keyword, so bare it would start a CASE expression.
        CheckConstraint(vocab.in_clause('"case"', vocab.FINDING_CASES), name="ck_legacy_findings_case"),
        CheckConstraint(
            f"kind IS NULL OR {vocab.in_clause('kind', vocab.KINDS)}", name="ck_legacy_findings_kind"
        ),
        CheckConstraint(vocab.in_clause("status", vocab.FINDING_STATUSES), name="ck_legacy_findings_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String, nullable=False)          # request or staff
    kind: Mapped[str | None] = mapped_column(String, nullable=True)       # request kind, null for staff
    subject_id: Mapped[str] = mapped_column(String, nullable=False)       # item id or staff id, as a string
    # The named case; "case" is a SQL keyword, so the column is quoted in DDL.
    case: Mapped[str] = mapped_column("case", String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)             # one plain sentence
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)   # what backs the finding

    # --- what HR did with it ---
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")   # open / resolved / ignored
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- bookkeeping ---
    # case:subject:kind:subject_id, unique so a rerun never duplicates a finding.
    fingerprint: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)    # the sync_runs row that found it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
