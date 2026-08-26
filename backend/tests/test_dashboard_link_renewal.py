"""Dashboard links must not expire just because nobody submitted a request.

A link lasts 30 days and is only minted while sending an email, so whether
someone keeps access depends on whether email happens to reach them. That decays
very differently by role — a manager is topped up by every approval request and
every reminder across their team, an employee only by their own activity — and
on a quiet team nobody is topped up at all and everyone silently loses access.

These cover the record that makes expiry visible and the task that acts on it.
"""

import asyncio
from datetime import timedelta

from app.models import DashboardLinkState
from app.models.mixins import utcnow
from app.services import dashboard_link_tracking as tracking
from app.services.dashboard_tokens import DEFAULT_EXPIRY_DAYS
from app.tasks import dashboard_links as task


async def _reset():
    """Drop every tracked row so each test starts from a known table.

    Also clears this feature's idempotency claims. A claim is deliberately
    one-shot per person per day, so without this the second test to renew the
    same employee would find the day already claimed by the first. Only our own
    namespace is cleared, leaving other suites' claims alone.
    """
    from sqlalchemy import delete

    from app.database import Base, async_session, engine
    from app.models import ProcessingLog

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as session:
        await session.execute(delete(DashboardLinkState))
        await session.execute(
            delete(ProcessingLog).where(ProcessingLog.list_id == task.CLAIM_NAMESPACE)
        )
        await session.commit()


async def _row(employee_id):
    from app.database import async_session

    async with async_session() as session:
        return await session.get(DashboardLinkState, str(employee_id))


# ----- recording a send -----

def test_recording_a_send_stores_when_the_link_dies():
    async def flow():
        await _reset()
        await tracking.record_link_sent("412")
        row = await _row("412")

        assert row is not None
        # The link's lifetime, not an arbitrary interval — the wording in the
        # renewal email and this column have to agree.
        gap = row.expires_at - row.last_sent_at
        assert abs(gap - timedelta(days=DEFAULT_EXPIRY_DAYS)) < timedelta(seconds=5)

    asyncio.run(flow())


def test_a_later_send_replaces_the_earlier_expiry():
    async def flow():
        await _reset()
        await tracking.record_link_sent("412", expiry_days=1)
        first = (await _row("412")).expires_at
        await tracking.record_link_sent("412", expiry_days=30)
        second = (await _row("412")).expires_at

        # One row per person, always describing their newest link.
        assert second > first

    asyncio.run(flow())


def test_a_failed_write_never_raises_at_the_caller(monkeypatch):
    # This runs immediately after an email has gone out. Bookkeeping must not be
    # able to turn a delivered notification into an error.
    def _boom(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(tracking, "async_session", _boom)
    asyncio.run(tracking.record_link_sent("412"))


# ----- finding who is about to lose access -----

def test_only_links_expiring_before_the_cutoff_are_returned():
    async def flow():
        await _reset()
        await tracking.record_link_sent("expiring-soon", expiry_days=2)
        await tracking.record_link_sent("plenty-of-time", expiry_days=25)

        due = await tracking.find_expiring(utcnow() + timedelta(days=7))

        assert due == ["expiring-soon"]

    asyncio.run(flow())


def test_the_most_urgent_come_first():
    async def flow():
        await _reset()
        await tracking.record_link_sent("later", expiry_days=5)
        await tracking.record_link_sent("sooner", expiry_days=1)

        due = await tracking.find_expiring(utcnow() + timedelta(days=7))

        assert due == ["sooner", "later"]

    asyncio.run(flow())


# ----- seeding people nobody has ever emailed -----

def test_seeding_covers_only_people_not_already_tracked():
    async def flow():
        await _reset()
        await tracking.record_link_sent("already-known")

        created = await tracking.seed_missing(["already-known", "never-seen"], 30)

        assert created == 1
        assert await _row("never-seen") is not None

    asyncio.run(flow())


def test_seeded_expiries_are_spread_rather_than_all_due_at_once():
    # Everyone due on day one would mean emailing the whole company through a
    # ten-per-minute rate limit on the first run after deployment.
    async def flow():
        await _reset()
        await tracking.seed_missing([str(i) for i in range(1, 31)], 30)

        due_now = await tracking.find_expiring(utcnow() + timedelta(days=7))

        assert len(due_now) < 30

    asyncio.run(flow())


def test_seeding_twice_does_not_move_anyone():
    async def flow():
        await _reset()
        await tracking.seed_missing(["7"], 30)
        first = (await _row("7")).expires_at
        assert await tracking.seed_missing(["7"], 30) == 0
        assert (await _row("7")).expires_at == first

    asyncio.run(flow())


def test_an_unparseable_id_still_gets_seeded():
    assert tracking._seed_offset("not-a-number", 30) == 0
    assert 0 <= tracking._seed_offset("41", 30) < 30


# ----- the task -----

def _employee(item_id, name="Someone", email="someone@ucsh.ca"):
    return {"id": str(item_id), "fields": {"Title": name, "EmailAddress": email}}


def _patch_task(monkeypatch, employees, sent):
    """Point the task at fake directory reads and capture what it would send."""
    class _Repo:
        async def get_all(self):
            return employees

    async def _get_employee_by_id(item_id):
        return next((e for e in employees if e["id"] == str(item_id)), None)

    async def _send(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr("app.repositories.factory.get_employee_repository", lambda: _Repo())
    monkeypatch.setattr("app.services.employee.get_employee_by_id", _get_employee_by_id)
    monkeypatch.setattr("app.graph.email.send_email_with_dashboard", _send)


def test_a_person_about_to_expire_is_emailed(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch_task(monkeypatch, [_employee(412)], sent)
        await tracking.record_link_sent("412", expiry_days=1)

        assert await task.renew_expiring_links() == 1

    asyncio.run(flow())
    assert sent[0]["to"] == ["someone@ucsh.ca"]
    # Sent through the footer path so it carries whichever dashboards they have
    # and records the new expiry, rather than building links of its own.
    assert sent[0]["primary_employee_id"] == "412"


def test_a_person_with_time_left_is_not_emailed(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch_task(monkeypatch, [_employee(412)], sent)
        await tracking.record_link_sent("412", expiry_days=25)

        assert await task.renew_expiring_links() == 0

    asyncio.run(flow())
    assert sent == []


def test_the_same_person_is_not_renewed_twice_on_one_day(monkeypatch):
    # Guards against two Railway replicas both deciding someone is due, and
    # against a restart part-way through a run.
    sent = []

    async def flow():
        await _reset()
        _patch_task(monkeypatch, [_employee(412)], sent)
        await tracking.record_link_sent("412", expiry_days=1)

        first = await task.renew_expiring_links()
        await tracking.record_link_sent("412", expiry_days=1)  # pretend it lapsed again
        second = await task.renew_expiring_links()

        assert (first, second) == (1, 0)

    asyncio.run(flow())
    assert len(sent) == 1


def test_someone_with_no_email_address_is_skipped(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch_task(monkeypatch, [_employee(412, email="")], sent)
        await tracking.record_link_sent("412", expiry_days=1)

        assert await task.renew_expiring_links() == 0

    asyncio.run(flow())
    assert sent == []


def test_seeding_alone_emails_nobody(monkeypatch):
    # A fresh deployment should bring people into scope quietly, not mail them.
    sent = []

    async def flow():
        await _reset()
        _patch_task(monkeypatch, [_employee(i) for i in range(1, 31)], sent)

        await task.renew_expiring_links()
        assert await tracking.known_employee_ids() == {str(i) for i in range(1, 31)}

    asyncio.run(flow())
    assert len(sent) < 30
