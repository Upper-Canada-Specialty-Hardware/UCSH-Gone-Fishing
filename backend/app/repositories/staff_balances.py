"""Read and write an employee's balance pots, from SharePoint or Postgres (#322).

The approval path moves balance pots (sick, make-up, carry-over, vacation,
payout) as part of one decision. It must not know which backend holds them, so
it asks this module for a ``StaffBalances`` and calls ``lock_pots`` then
``write_pots``. Two implementations answer the same protocol:

- SharePoint (here): the Staff Directory is the record. It cannot take part in
  the database transaction, so the approval path writes the ledger key first,
  then SharePoint, then commits (see ``SharePointStaffBalances.write_pots``).
- Postgres (``app/repositories/postgres/staff_balances.py``, added by the staff
  work): a ``SELECT ... FOR UPDATE`` on the staff row inside the caller's
  session, the real one-transaction path.

``get_staff_balances`` picks one per call from the staff store's
``staff_in_postgres`` (the ``staff_storage`` setting, or the
``STORAGE_EMPLOYEES=postgres`` override), so the switch flips without a
redeploy.
"""
import logging
from typing import Protocol

from app.config import settings
from app.graph.sharepoint import sp_client
from app.repositories.postgres.staff_balances import PostgresStaffBalances
# The one definition of "staff lives in Postgres", re-exported for callers here.
from app.repositories.staff_store import staff_in_postgres  # noqa: F401

logger = logging.getLogger(__name__)

# The ledger pot names and the Staff Directory column each one lives in.
# vacation_taken is not here: it is the hourly counter, summed from ledger rows,
# never stored on the staff record.
POT_COLUMNS = {
    "sick": "CurrentSickDayBalance",
    "make_up": "CurrentOvertimeBalance",
    "carry_over": "CarryOver",
    "vacation": "CurrentVacationBalance",
    "payout": "Payout",
}


class StaffBalances(Protocol):
    """Read and write one person's balance pots within a decision."""

    async def lock_pots(self, session, employee_id: str) -> dict[str, float]:
        """Current pots for one person, locked for this transaction where the backend can.

        Args:
            session: The decision's open session (used by the Postgres backend
                to lock the row; the SharePoint backend ignores it).
            employee_id: Whose pots to read.

        Returns:
            ``{pot: value}`` for the five spendable pots.
        """
        ...

    async def write_pots(self, session, employee_id: str, new_values: dict[str, float]) -> None:
        """Set the given pots. No commit; the caller's transaction carries it.

        Args:
            session: The decision's open session.
            employee_id: Whose pots to set.
            new_values: ``{pot: new_value}`` for each pot that moved.
        """
        ...


class SharePointStaffBalances:
    """Balance pots kept on the Staff Directory, read and written through Graph."""

    async def lock_pots(self, session, employee_id: str) -> dict[str, float]:
        """Read one person's pots from the Staff Directory.

        SharePoint cannot join the database transaction, so there is no real
        lock here; the approval path serialises one employee's decisions with
        the per-employee asyncio lock instead (services/concurrency.py).

        Args:
            session: Unused for this backend (the Postgres one locks the row).
            employee_id: Whose pots to read.

        Returns:
            ``{pot: value}`` for the five pots; missing columns read as 0.
        """
        staff = await sp_client.get_list_item_or_none(settings.SP_LIST_STAFF_DIRECTORY, employee_id)
        fields = staff["fields"] if staff else {}
        return {pot: float(fields.get(col, 0) or 0) for pot, col in POT_COLUMNS.items()}

    async def write_pots(self, session, employee_id: str, new_values: dict[str, float]) -> None:
        """Write the moved pots back to the Staff Directory.

        This write does NOT join the database transaction. The approval path
        therefore checks the ledger idempotency key BEFORE calling this and
        commits the ledger AFTER, so a retry of an already-applied decision
        never moves a balance twice. One window remains and is accepted: if the
        process dies after this SharePoint write but before the database commit,
        SharePoint is moved while the ledger is not, and a retry would move it
        again. That window is why staff and requests move to Postgres together;
        the Postgres backend does this write inside the same transaction and
        closes it.

        Args:
            session: Unused for this backend.
            employee_id: Whose pots to set.
            new_values: ``{pot: new_value}`` for each pot that moved.
        """
        updates = {POT_COLUMNS[pot]: value for pot, value in new_values.items() if pot in POT_COLUMNS}
        if updates:                                                # nothing to do when no pot moved
            await sp_client.update_list_item_fields(settings.SP_LIST_STAFF_DIRECTORY, employee_id, updates)


async def get_staff_balances() -> StaffBalances:
    """The balance backend to use now: SharePoint or Postgres.

    Chosen per call so a flip of the switch takes effect without a redeploy.

    Returns:
        A ``StaffBalances`` implementation.
    """
    if await staff_in_postgres():                              # same answer the staff store gives
        return PostgresStaffBalances()                         # the real one-transaction path
    return SharePointStaffBalances()
