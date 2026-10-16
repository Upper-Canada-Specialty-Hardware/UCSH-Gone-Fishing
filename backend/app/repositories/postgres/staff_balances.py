"""The five balance pots read and written in one Postgres transaction (#261, #322).

This is the Postgres side of the ``StaffBalances`` interface the approval path
depends on (the SharePoint side lives in ``app/repositories/staff_balances.py``).
It is the real one-transaction path: ``lock_pots`` takes the staff row with
``SELECT ... FOR UPDATE`` so two concurrent approvals for the same person
serialise, and ``write_pots`` sets the new values in the caller's own session
(no commit of its own, so the status change, the ledger rows, the events and the
balance move all land together or not at all). Each write also queues a
write-back, so the moved balance reaches the SharePoint Staff Directory list the
Bulletin Board and the Microsoft Form still read (#267).
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.staff import Staff

logger = logging.getLogger(__name__)

# Ledger pot name -> Staff model attribute, and -> SharePoint column for the
# write-back. The approval path spends and credits by these pot names.
_POT_TO_ATTR = {
    "sick": "sick_balance",
    "make_up": "overtime_balance",
    "carry_over": "carry_over",
    "vacation": "vacation_balance",
    "payout": "payout",
}
_POT_TO_SP_COLUMN = {
    "sick": "CurrentSickDayBalance",
    "make_up": "CurrentOvertimeBalance",
    "carry_over": "CarryOver",
    "vacation": "CurrentVacationBalance",
    "payout": "Payout",
}


def _as_float(value) -> float:
    """A stored pot value as a float, blank counting as zero.

    Args:
        value: The column value (float, number string, or None).

    Returns:
        The value as a float, or 0.0 when blank.
    """
    try:
        return float(value or 0)                          # None/""/0 -> 0.0
    except (TypeError, ValueError):
        return 0.0


class PostgresStaffBalances:
    """The five pots in Postgres, locked and written inside the caller's transaction."""

    async def lock_pots(self, session: AsyncSession, employee_id: str) -> dict[str, float]:
        """The person's five pots, locked for this transaction where the backend can.

        On Postgres the staff row is taken ``FOR UPDATE`` so a second approval for
        the same person waits; on SQLite (one writer at a time anyway) it is a
        plain select, which behaves the same for one process.

        Args:
            session: The caller's open session; the lock lives on its transaction.
            employee_id: The person's SharePoint item id, as a string.

        Returns:
            ``{pot: value}`` for the five pots, every value a float (0.0 when blank).

        Raises:
            LookupError: When no staff row has that id.
        """
        statement = select(Staff).where(Staff.sp_item_id == str(employee_id))
        if session.bind.dialect.name == "postgresql":     # real row lock only where it exists
            statement = statement.with_for_update()
        row = (await session.execute(statement)).scalars().first()
        if row is None:                                   # the caller must know the person exists
            raise LookupError(f"No staff row for employee {employee_id}")
        return {pot: _as_float(getattr(row, attr)) for pot, attr in _POT_TO_ATTR.items()}

    async def write_pots(self, session: AsyncSession, employee_id: str, new_values: dict[str, float]) -> None:
        """Set the given pots on the staff row and queue a write-back. No commit.

        Only the pots named in ``new_values`` move; the rest are left as they are.
        The write-back carries the SharePoint columns for the pots that changed,
        so the Staff Directory list catches up in the background.

        Args:
            session: The caller's open session; the caller commits it.
            employee_id: The person's SharePoint item id, as a string.
            new_values: ``{pot: new value}`` for the pots to set.

        Raises:
            LookupError: When no staff row has that id.
            KeyError: On a pot name outside the five, so a typo fails loudly
                rather than silently dropping a balance move.
        """
        from app.services.staff_writeback import enqueue_writeback   # avoid an import cycle

        row = (await session.execute(
            select(Staff).where(Staff.sp_item_id == str(employee_id))
        )).scalars().first()
        if row is None:
            raise LookupError(f"No staff row for employee {employee_id}")
        writeback_fields: dict = {}                       # the SharePoint columns that moved
        for pot, value in new_values.items():
            if pot not in _POT_TO_ATTR:                   # an unknown pot is a programming error
                raise KeyError(f"Unknown balance pot {pot!r}")
            setattr(row, _POT_TO_ATTR[pot], float(value))  # set the typed column
            writeback_fields[_POT_TO_SP_COLUMN[pot]] = float(value)
        # Queue the write-back in the same session, so it commits with the move
        # (or not at all); a pulled edit never reaches here, so there is no echo.
        enqueue_writeback(session, str(employee_id), writeback_fields)
