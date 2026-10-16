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

``get_staff_balances`` picks one per call from the ``staff_storage`` setting, so
the switch flips without a redeploy. Until the staff work is joined the setting
does not exist and this returns the SharePoint implementation.
"""
import logging
from typing import Protocol

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models.app_setting import AppSetting

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


async def staff_in_postgres() -> bool:
    """Whether the Staff Directory's balances live in Postgres now.

    The ``STORAGE_EMPLOYEES`` config value is an override (tests and operators
    pin it); otherwise the ``staff_storage`` app setting decides. The setting
    does not exist until the staff work adds it, and then the default is
    SharePoint, so this reads it defensively rather than through
    ``app_settings.get_setting`` (which only knows registered keys).

    Returns:
        True once staff balances should be read and written in Postgres.
    """
    override = (getattr(settings, "STORAGE_EMPLOYEES", "") or "").strip().lower()
    if override == "postgres":
        return True                                            # the environment pins Postgres
    if override == "sharepoint":
        return False                                           # the environment pins SharePoint
    async with async_session() as session:                     # otherwise the live app setting decides
        row = await session.get(AppSetting, "staff_storage")
    return bool(row) and str(row.value).strip().lower() == "postgres"


async def get_staff_balances() -> StaffBalances:
    """The balance backend to use now: SharePoint or Postgres.

    Chosen per call so a flip of the switch takes effect without a redeploy.
    Falls back to SharePoint when staff is not in Postgres, or when the Postgres
    implementation is not present yet (the staff work has not been joined).

    Returns:
        A ``StaffBalances`` implementation.
    """
    if await staff_in_postgres():
        try:
            from app.repositories.postgres.staff_balances import PostgresStaffBalances
            return PostgresStaffBalances()                     # the real one-transaction path
        except ImportError:
            # The staff work has not landed its Postgres implementation yet.
            logger.warning("staff_storage is postgres but the Postgres staff balances are missing; using SharePoint")
    return SharePointStaffBalances()
