"""Backfill engine: idempotent SharePoint -> Postgres upsert and a read-only
verify diff.

The ``DOMAINS`` registry pairs each domain with (a) the repository call that
yields its SharePoint items, (b) the destination model, and (c) the mapper that
turns an SP item into that model's column values. ``upsert_domain`` and
``diff_domain`` are the two operations the CLI drives; both key on
``sp_item_id``.

Two shapes of domain live here:

* **List-shaped** (``DOMAINS``) — one SP list item becomes one PG row, keyed on
  ``sp_item_id``: employees, holidays, and the three request lists.
* **Derived** (``DERIVED_DOMAINS``) — ``manager_assignments``, which has no list
  of its own. Each employee's managers are a multi-value person field inline on
  the Staff Directory record, so one SP item yields N rows and the key is the
  ``(employee, manager)`` pair rather than an ``sp_item_id``. It therefore
  carries its own upsert/diff instead of reusing the generic ones.

Because the derived rows point at ``employees.id``, ``manager_assignments`` is
always processed *after* ``employees`` — ``resolve_domains`` enforces that order
regardless of the order the caller asks for.
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
    ManagerAssignment,
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


# ------------------------- derived: manager_assignments -------------------------


@dataclass
class DerivedDomain:
    """A domain with no SP list of its own, so it brings its own upsert/diff."""
    name: str
    fetch: Callable[[], Awaitable[list[dict]]]
    upsert: Callable                             # (session, items) -> report dict
    diff: Callable                               # (session, items) -> report dict


async def _employee_id_by_sp_item_id(session) -> dict[str, int]:
    """Map Staff Directory item id -> the ``employees.id`` it was backfilled to.

    ``manager_assignments.employee_id`` is a Postgres foreign key, but SharePoint
    only knows the list item id, so every derived row needs this translation.

    Args:
        session: An open AsyncSession.

    Returns:
        ``{sp_item_id: employees.id}`` for every backfilled employee.
    """
    rows = (await session.execute(select(Employee.sp_item_id, Employee.id))).all()
    return {sp_item_id: pk for sp_item_id, pk in rows}


async def upsert_manager_assignments(session, items: list[dict]) -> dict:
    """Replace each employee's manager edges to match SharePoint exactly.

    This is a *set replacement* per employee, not an insert-only pass: a manager
    removed in SharePoint must disappear here too, or the ex-manager keeps
    receiving that employee's approvals forever. Employees absent from the
    ``employees`` table are skipped and reported rather than failing the run —
    they simply have not been backfilled yet.

    Args:
        session: An open AsyncSession; committed once at the end.
        items: Staff Directory items as returned by the employee repository.

    Returns:
        Counts of rows inserted/updated/deleted plus
        ``employees_missing_in_postgres`` — the SP item ids that had managers but
        no employee row to hang them on.
    """
    employee_ids = await _employee_id_by_sp_item_id(session)
    inserted = updated = deleted = 0
    skipped: list[str] = []

    for item in items:
        sp_id = str(item["id"])
        employee_id = employee_ids.get(sp_id)
        desired = mappers.map_manager_assignments(item)
        if employee_id is None:
            # No FK target — record it only if there was actually work to do.
            if desired:
                skipped.append(sp_id)
            continue

        # Keyed by manager so existing rows can be matched, updated, or dropped.
        wanted_by_manager = {row["manager_sp_user_lookup_id"]: row for row in desired}
        existing = (await session.execute(
            select(ManagerAssignment).where(ManagerAssignment.employee_id == employee_id)
        )).scalars().all()

        for row in existing:
            # pop: what remains afterwards is genuinely new.
            wanted = wanted_by_manager.pop(row.manager_sp_user_lookup_id, None)
            if wanted is None:
                # Manager no longer listed in SharePoint — drop the stale edge.
                await session.delete(row)
                deleted += 1
                continue
            row.manager_name = wanted["manager_name"]
            row.position = wanted["position"]  # order may have changed
            updated += 1

        for wanted in wanted_by_manager.values():
            session.add(ManagerAssignment(employee_id=employee_id, **wanted))
            inserted += 1

    await session.commit()
    return {
        "total_sharepoint": len(items),
        "inserted": inserted,
        "updated": updated,
        "deleted": deleted,
        "employees_missing_in_postgres": skipped,
    }


async def diff_manager_assignments(session, items: list[dict]) -> dict:
    """Read-only parity check for the derived manager edges. Performs NO writes.

    Mirrors ``diff_domain`` but keys on the ``(employee, manager)`` pair, and
    adds ``employees_missing_in_postgres`` — employees whose edges cannot exist
    yet because the employee row itself has not been backfilled. That counts as
    out-of-parity: flipping the employees flag in that state would serve an
    employee with no managers at all.

    Args:
        session: An open AsyncSession.
        items: Staff Directory items as returned by the employee repository.

    Returns:
        A report dict with ``missing_in_postgres``, ``field_mismatches``,
        ``orphans_in_postgres``, ``employees_missing_in_postgres`` and the
        ``in_parity`` roll-up the CLI exit code gates on.
    """
    employee_ids = await _employee_id_by_sp_item_id(session)
    # Reverse map so orphans are reported by SP item id, not an internal PK.
    sp_id_by_employee_id = {pk: sp_id for sp_id, pk in employee_ids.items()}

    missing: list[dict] = []
    mismatched: list[dict] = []
    skipped: list[str] = []
    seen_pairs: set[tuple[int, int]] = set()

    for item in items:
        sp_id = str(item["id"])
        employee_id = employee_ids.get(sp_id)
        desired = mappers.map_manager_assignments(item)
        if employee_id is None:
            if desired:
                skipped.append(sp_id)
            continue

        existing = {
            row.manager_sp_user_lookup_id: row
            for row in (await session.execute(
                select(ManagerAssignment).where(ManagerAssignment.employee_id == employee_id)
            )).scalars().all()
        }

        for wanted in desired:
            manager_id = wanted["manager_sp_user_lookup_id"]
            seen_pairs.add((employee_id, manager_id))
            row = existing.get(manager_id)
            if row is None:
                missing.append({
                    "employee_sp_item_id": sp_id,
                    "manager_sp_user_lookup_id": manager_id,
                })
                continue
            field_diffs = {
                key: {"sharepoint": value, "postgres": getattr(row, key)}
                for key, value in wanted.items()
                if getattr(row, key) != value
            }
            if field_diffs:
                mismatched.append({
                    "employee_sp_item_id": sp_id,
                    "manager_sp_user_lookup_id": manager_id,
                    "fields": field_diffs,
                })

    all_pairs = set((await session.execute(select(
        ManagerAssignment.employee_id, ManagerAssignment.manager_sp_user_lookup_id
    ))).all())
    orphans = [
        {
            # None when the employee row itself is gone — still worth surfacing.
            "employee_sp_item_id": sp_id_by_employee_id.get(employee_id),
            "manager_sp_user_lookup_id": manager_id,
        }
        # Sorted on strings so a missing sp_item_id (None) cannot break ordering.
        for employee_id, manager_id in sorted(
            all_pairs - seen_pairs,
            key=lambda pair: (str(sp_id_by_employee_id.get(pair[0]) or ""), pair[1]),
        )
    ]

    return {
        "total_sharepoint": len(items),
        "total_postgres": len(all_pairs),
        "missing_in_postgres": missing,
        "field_mismatches": mismatched,
        "orphans_in_postgres": orphans,
        "employees_missing_in_postgres": skipped,
        "in_parity": not (missing or mismatched or orphans or skipped),
    }


DERIVED_DOMAINS: dict[str, DerivedDomain] = {
    "manager_assignments": DerivedDomain(
        "manager_assignments",
        # Same Staff Directory fetch as employees — AllManagers rides along on
        # the item, so this costs no extra Graph call beyond the one list read.
        lambda: get_employee_repository().get_all(),
        upsert_manager_assignments, diff_manager_assignments,
    ),
}

# Ordered: employees before manager_assignments, which depends on employees.id.
ALL_DOMAINS: dict[str, Domain | DerivedDomain] = {**DOMAINS, **DERIVED_DOMAINS}


def resolve_domains(names: list[str] | None) -> list[Domain | DerivedDomain]:
    """Names -> domain objects; None/empty means every domain.

    Args:
        names: Domain names to run, or None/empty for all of them.

    Returns:
        The matching domains, re-ordered to ``ALL_DOMAINS`` order so
        ``manager_assignments`` never runs before ``employees`` — otherwise every
        edge would be skipped for want of a foreign-key target.

    Raises:
        ValueError: If a name is not a known domain.
    """
    if not names:
        return list(ALL_DOMAINS.values())
    for name in names:
        if name not in ALL_DOMAINS:
            raise ValueError(f"Unknown domain '{name}'. Known: {', '.join(ALL_DOMAINS)}")
    # Filter ALL_DOMAINS rather than iterating `names`, so dependency order wins
    # over the order the caller happened to pass the flags in.
    return [domain for name, domain in ALL_DOMAINS.items() if name in set(names)]


async def run(domain_names: list[str] | None = None, apply: bool = False) -> dict:
    """Run verify (default) or apply across the selected domains, opening one DB
    session for the run. Returns a per-domain report dict.
    """
    domains = resolve_domains(domain_names)
    report: dict = {"mode": "apply" if apply else "verify", "domains": {}}
    async with async_session() as session:
        for domain in domains:
            items = await domain.fetch()
            if isinstance(domain, DerivedDomain):
                # Derived domains key on a pair, not sp_item_id, so they supply
                # their own upsert/diff rather than using the generic ones.
                result = await (domain.upsert if apply else domain.diff)(session, items)
            elif apply:
                result = await upsert_domain(session, domain, items)
            else:
                result = await diff_domain(session, domain, items)
            report["domains"][domain.name] = result
            logger.info("backfill %s %s: %s", report["mode"], domain.name, result)
    return report
