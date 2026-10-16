"""The legacy data handler: recording, listing and resolving findings (#337).

A finding is written once per fingerprint (a rerun never duplicates it), listed
with filters and per-case counts, and marked resolved or ignored only with a
note, recording who and when. The admin legacy endpoints sit behind the admin
password like every other /admin/* route. The test database persists between
runs, so every finding here uses a fresh subject id and nothing asserts a global
total.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import async_session
from app.models.legacy_finding import LegacyFinding
from app.services import legacy_findings


def _uid() -> str:
    """A subject id no earlier run has used, so fingerprints never collide."""
    return f"test-{uuid.uuid4().hex}"


async def _write(case: str, subject: str, kind: str | None, subject_id: str, reason: str, evidence: dict, run_id: int | None = None):
    """Record one finding in its own committed transaction, returning the row's id."""
    async with async_session() as session:
        row = await legacy_findings.record_finding(session, case, subject, kind, subject_id, reason, evidence, run_id)
        await session.commit()
        return row.id if row is not None else None


def test_record_finding_is_idempotent_on_the_fingerprint():
    """The same case/subject/kind/subject_id files once; the rerun is a no-op."""
    subject_id = _uid()

    async def run():
        async with async_session() as session:
            first = await legacy_findings.record_finding(
                session, "date_error", "request", "leave", subject_id,
                "start date is after end date", {"start": "2026-11-05", "end": "2026-11-02"}, 1,
            )
            second = await legacy_findings.record_finding(
                session, "date_error", "request", "leave", subject_id,
                "start date is after end date", {}, 2,
            )
            await session.commit()
            return first, second

    first, second = asyncio.run(run())
    assert first is not None                                   # the first insert lands
    assert second is None                                      # the rerun is skipped

    async def rows_for():
        async with async_session() as session:
            result = await session.execute(select(LegacyFinding).where(LegacyFinding.subject_id == subject_id))
            return result.scalars().all()

    rows = asyncio.run(rows_for())
    assert len(rows) == 1                                      # exactly one row survives
    assert rows[0].case == "date_error"
    assert rows[0].status == "open"


def test_list_applies_case_status_and_subject_filters():
    """A listing keeps only the chosen case, status and subject."""
    req_id = _uid()
    staff_id = _uid()
    asyncio.run(_write("submitter_unmatched", "request", "leave", req_id, "submitter not matched to a staff record", {}))
    asyncio.run(_write("username_as_name", "staff", None, staff_id, "a sign-in name is stored as the person's name", {}))

    open_all = asyncio.run(legacy_findings.list_findings(status="open", page_size=500))
    ids = {f["subject_id"] for f in open_all["findings"]}
    assert req_id in ids and staff_id in ids                   # both show among the open findings

    by_case = asyncio.run(legacy_findings.list_findings(case="submitter_unmatched", status="open", page_size=500))
    case_ids = {f["subject_id"] for f in by_case["findings"]}
    assert req_id in case_ids and staff_id not in case_ids     # the case filter keeps only its own

    by_subject = asyncio.run(legacy_findings.list_findings(subject="staff", status="open", page_size=500))
    subject_ids = {f["subject_id"] for f in by_subject["findings"]}
    assert staff_id in subject_ids and req_id not in subject_ids   # the subject filter keeps only staff


def test_counts_by_case_counts_the_chosen_status():
    """Filing one more open finding of a case lifts that case's open count by one."""
    before = asyncio.run(legacy_findings.counts_by_case(status="open"))
    asyncio.run(_write("no_user_link", "staff", None, _uid(), "record has no link to a directory user", {}))
    after = asyncio.run(legacy_findings.counts_by_case(status="open"))
    assert after["no_user_link"] == before.get("no_user_link", 0) + 1


def test_resolve_and_ignore_require_a_note_and_record_who():
    """Resolving needs a note; it stores the status, the note, who and when."""
    finding_id = asyncio.run(_write("hourly_vacation_leftover", "staff", None, _uid(), "hourly worker still has a vacation balance", {"vacation": 3}))

    with pytest.raises(legacy_findings.FindingError):
        asyncio.run(legacy_findings.resolve(finding_id, "   "))   # an empty note is refused

    resolved = asyncio.run(legacy_findings.resolve(finding_id, "zeroed the balance in the person panel", actor="Jordan Lee"))
    assert resolved["status"] == "resolved"
    assert resolved["note"] == "zeroed the balance in the person panel"
    assert resolved["resolved_by"] == "Jordan Lee"             # who decided is recorded
    assert resolved["resolved_at"]                             # and when

    # Ignore records the shared admin actor when none is named.
    ignore_id = asyncio.run(_write("below_zero_predates_rules", "staff", None, _uid(), "balance went below zero before the hourly rules", {}))
    ignored = asyncio.run(legacy_findings.ignore(ignore_id, "expected for this legacy record"))
    assert ignored["status"] == "ignored"
    assert ignored["resolved_by"] == legacy_findings.DEFAULT_ACTOR


def test_resolve_missing_finding_raises_not_found():
    """Resolving an id that is not there is a 404-style error."""
    with pytest.raises(legacy_findings.FindingError) as caught:
        asyncio.run(legacy_findings.resolve(999_000_111, "note"))
    assert caught.value.status_code == 404


def test_the_legacy_endpoints_sit_behind_the_admin_guard():
    """The list, resolve and ignore routes are published under the admin path and
    refuse a call with no admin session, like every other /admin/* route (#228)."""
    from app.main import app
    from app.services import admin_auth

    client = TestClient(app)                                   # no `with`: startup tasks stay off
    published = [p for p in app.openapi()["paths"] if p.startswith(admin_auth.ADMIN_PATH + "legacy")]
    assert published, "the admin legacy routes should be published"
    assert client.get("/api/dashboard/admin/legacy/findings").status_code == 401
    assert client.post("/api/dashboard/admin/legacy/findings/1/resolve", json={"note": "x"}).status_code == 401
    assert client.post("/api/dashboard/admin/legacy/findings/1/ignore", json={"note": "x"}).status_code == 401
