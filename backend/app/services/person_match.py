"""Match a request's submitter or manager to a staff record, by id then email (#265).

Once the Staff Directory lives in Postgres, a person is matched by their staff
id first and then by email, and by nothing else. There is deliberately no
display-name match, no Microsoft 365 user-list lookup, and no SharePoint site
membership check: those were the fragile parts of the old person-column path,
and the owner ruled them out. Email is compared case- and space-insensitively,
the same normalisation the ``staff`` table stores, so stray spacing or casing in
a form submission still finds the person.

The match returns the record in the SharePoint item shape
(``{"id", "fields"}``), so it is a drop-in for ``get_employee_by_email`` and the
rest of the app reads it exactly as before.
"""
import logging

from app.database import async_session
from app.models.staff import Staff
from app.repositories.postgres.staff import _norm_email, item_shape, staff_query

logger = logging.getLogger(__name__)


async def match_person(staff_id: str | int | None = None, email: str | None = None) -> dict | None:
    """Find a staff record by id first, then by email. No name or M365 match.

    Args:
        staff_id: The person's Staff Directory item id, if known.
        email: The person's email, matched case- and space-insensitively.

    Returns:
        The staff record as ``{"id", "fields"}``, or None when neither the id nor
        the email leads to one.
    """
    async with async_session() as session:
        if staff_id not in (None, ""):                     # id wins when it is given
            by_id = (await session.execute(
                staff_query().where(Staff.sp_item_id == str(staff_id))
            )).scalars().first()
            if by_id is not None:
                return item_shape(by_id)
        norm = _norm_email(email)                          # trimmed, spaceless, lower case
        if norm:
            by_email = (await session.execute(
                staff_query().where(Staff.email == norm)
            )).scalars().first()
            if by_email is not None:
                return item_shape(by_email)
    logger.info("person_match found no staff for id=%r email=%r", staff_id, email)
    return None
