"""Backfill engine: idempotent SharePoint -> Postgres upsert and a read-only
verify diff.

The ``DOMAINS`` registry pairs each domain with (a) the repository call that
yields its SharePoint items, (b) the destination model, and (c) the mapper that
turns an SP item into that model's column values. ``upsert_domain`` and
``diff_domain`` are the two operations the CLI drives; both key on
``sp_item_id``.

Scope note: this covers the five list-shaped domains that map one SP list to one
PG table (employees, holidays, and the three request lists). ``manager_assignments``
is intentionally excluded — it is *derived* from the Staff Directory ``AllManagers``
person field rather than a flat list, so it is built alongside its own cutover
(PR F), not here.
"""
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import select

from app.backfill import mappers
from app.database import async_session
from app.models import (
    CarryoverPayoutRequest,
    Employee,
    Holiday,
    LeaveRequest,
    OvertimeRequest,
)
from app.repositories import (
    get_carryover_payout_repository,
    get_employee_repository,
    get_holiday_repository,
    get_leave_request_repository,
    get_overtime_request_repository,
)

logger = logging.getLogger(__name__)


@dataclass
class Domain:
    name: str
    model: type
    fetch: Callable[[], Awaitable[list[dict]]]  # yields SharePoint items
    map_item: Callable[[dict], dict]            # SP item -> PG column values


DOMAINS: dict[str, Domain] = {
    "employees": Domain(
        "employees", Employee,
        lambda: get_employee_repository().get_all(), mappers.map_employee,
    ),
    "holidays": Domain(
        "holidays", Holiday,
        lambda: get_holiday_repository().get_all(), mappers.map_holiday,
    ),
    "leave_requests": Domain(
        "leave_requests", LeaveRequest,
        lambda: get_leave_request_repository().get_all(), mappers.map_leave_request,
    ),
    "overtime_requests": Domain(
        "overtime_requests", OvertimeRequest,
        lambda: get_overtime_request_repository().get_all(), mappers.map_overtime_request,
    ),
    "carryover_payout_requests": Domain(
        "carryover_payout_requests", CarryoverPayoutRequest,
        lambda: get_carryover_payout_repository().get_all(), mappers.map_carryover_payout_request,
    ),
}


async def upsert_domain(session, domain: Domain, items: list[dict]) -> dict:
    """Idempotently write mapped ``items`` into ``domain.model``, keyed on
    ``sp_item_id``: existing rows are updated in place, new rows inserted. Safe
    to re-run — a second pass with the same data produces no duplicates.
    """
    inserted = updated = 0
    for item in items:
        values = domain.map_item(item)
        sp_id = values["sp_item_id"]
        existing = (await session.execute(
            select(domain.model).where(domain.model.sp_item_id == sp_id)
        )).scalar_one_or_none()
        if existing is None:
            session.add(domain.model(**values))
            inserted += 1
        else:
            for key, value in values.items():
                setattr(existing, key, value)
            updated += 1
    await session.commit()
    return {"total_sharepoint": len(items), "inserted": inserted, "updated": updated}


def _norm(value):
    """Normalize for comparison so equal-but-differently-typed values match
    (float precision after a DB round-trip is the main case)."""
    if isinstance(value, float):
        return round(value, 6)
    return value


async def diff_domain(session, domain: Domain, items: list[dict]) -> dict:
    """Read-only parity check: for each SP item confirm a matching Postgres row
    with equal mapped values. Reports rows missing from Postgres, per-field
    mismatches, and Postgres rows with no SharePoint counterpart (orphans).
    Performs NO writes.
    """
    sp_ids: set[str] = set()
    missing: list[str] = []
    mismatched: list[dict] = []
    for item in items:
        values = domain.map_item(item)
        sp_id = values["sp_item_id"]
        sp_ids.add(sp_id)
        existing = (await session.execute(
            select(domain.model).where(domain.model.sp_item_id == sp_id)
        )).scalar_one_or_none()
        if existing is None:
            missing.append(sp_id)
            continue
        field_diffs = {
            key: {"sharepoint": value, "postgres": getattr(existing, key)}
            for key, value in values.items()
            if _norm(getattr(existing, key)) != _norm(value)
        }
        if field_diffs:
            mismatched.append({"sp_item_id": sp_id, "fields": field_diffs})

    all_pg_ids = set(
        (await session.execute(select(domain.model.sp_item_id))).scalars()
    )
    orphans = sorted(all_pg_ids - sp_ids)

    return {
        "total_sharepoint": len(items),
        "total_postgres": len(all_pg_ids),
        "missing_in_postgres": missing,
        "field_mismatches": mismatched,
        "orphans_in_postgres": orphans,
        "in_parity": not (missing or mismatched or orphans),
    }


def resolve_domains(names: list[str] | None) -> list[Domain]:
    """Names -> Domain objects; None/empty means every domain."""
    if not names:
        return list(DOMAINS.values())
    resolved = []
    for name in names:
        if name not in DOMAINS:
            raise ValueError(f"Unknown domain '{name}'. Known: {', '.join(DOMAINS)}")
        resolved.append(DOMAINS[name])
    return resolved


async def run(domain_names: list[str] | None = None, apply: bool = False) -> dict:
    """Run verify (default) or apply across the selected domains, opening one DB
    session for the run. Returns a per-domain report dict.
    """
    domains = resolve_domains(domain_names)
    report: dict = {"mode": "apply" if apply else "verify", "domains": {}}
    async with async_session() as session:
        for domain in domains:
            items = await domain.fetch()
            if apply:
                result = await upsert_domain(session, domain, items)
            else:
                result = await diff_domain(session, domain, items)
            report["domains"][domain.name] = result
            logger.info("backfill %s %s: %s", report["mode"], domain.name, result)
    return report
