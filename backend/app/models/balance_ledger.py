"""Every movement of a balance pot, one row each, kept forever (#322).

A ledger row records one pot changing for one employee: the amount, the value
before and after, why, who did it and on which channel. Approvals, refunds and
hand edits all write here, so a person's balance history can be replayed and a
payroll dispute answered from the record rather than from today's totals. The
``idempotency_key`` makes each movement land at most once across channels,
instances and reruns.

Rows are only ever inserted, never changed or deleted.
"""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import request_vocab as vocab
from app.models.mixins import utcnow


class BalanceLedger(Base):
    """One pot movement for one employee."""

    __tablename__ = "balance_ledger"
    __table_args__ = (
        CheckConstraint(vocab.in_clause("pot", vocab.POTS), name="ck_balance_ledger_pot"),
        CheckConstraint(vocab.in_clause("channel", vocab.CHANNELS), name="ck_balance_ledger_channel"),
        # Every history read is one person, newest first.
        Index("ix_balance_ledger_employee_created", "employee_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The request this movement belongs to; null for a hand edit with no request.
    # ON DELETE SET NULL: deleting a request keeps its balance history (a payroll
    # dispute is answered from the ledger), with request_id cleared (#282, finding 4).
    request_id: Mapped[int | None] = mapped_column(
        ForeignKey("requests.id", ondelete="SET NULL"), nullable=True
    )
    # Whose balance moved; every history read filters by one person, so indexed.
    employee_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    pot: Mapped[str] = mapped_column(String, nullable=False)                 # which pot moved
    # Numeric(12, 4): SharePoint balances carry up to 4 decimals (hours / 8), so
    # a ledger row keeps the exact before/after/change without rounding (#335 fix).
    change: Mapped[float] = mapped_column(Numeric(12, 4, asdecimal=False), nullable=False)   # after - before
    before: Mapped[float] = mapped_column(Numeric(12, 4, asdecimal=False), nullable=False)   # pot value before
    after: Mapped[float] = mapped_column(Numeric(12, 4, asdecimal=False), nullable=False)    # pot value after
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)          # one plain sentence
    actor: Mapped[str | None] = mapped_column(String, nullable=True)         # who caused it
    channel: Mapped[str] = mapped_column(String, nullable=False)             # where it came from
    # The step key; unique so the same movement never posts twice.
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
