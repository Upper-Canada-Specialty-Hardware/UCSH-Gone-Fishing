"""The in-process fan-out of request-timeline events to live readers (#328).

Every approval step writes a row to ``request_events`` and, on Postgres, fires
``pg_notify('request_events', <event id>)`` at commit (see request_journal).
This bus is the other half: one process-wide listener turns those notifies into
messages that each open dashboard connection receives, so an approval by email,
text or another dashboard shows on every open page within a second or two.

How it listens depends on the database:

- Postgres: one dedicated asyncpg connection runs ``LISTEN request_events``.
  Each notify (and each reconnect) triggers a read of every event newer than the
  last one dispatched, so a notify lost during a blip is caught on reconnect.
- SQLite, or whenever LISTEN cannot be used: the same read runs on a 2 second
  poll instead, so tests and local dev behave the same way without NOTIFY.

A reader ``subscribe()``s to get an ``asyncio.Queue`` the bus pushes payloads
onto, and ``unsubscribe()``s when its connection closes. The bus only ever
dispatches events newer than ``_last_id``, which starts at the newest event at
startup; a connection replays older history itself through its Last-Event-ID.
"""
import asyncio
import json
import logging

from sqlalchemy import func, select

from app.database import async_session, engine
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.services.request_journal import NOTIFY_CHANNEL

logger = logging.getLogger(__name__)

# How often the SQLite/fallback path checks request_events for new rows.
POLL_SECONDS = 2
# Reconnect backoff for the Postgres listener: grows, then holds at the cap.
RECONNECT_MIN_SECONDS = 1
RECONNECT_MAX_SECONDS = 30
# Most events read in one dispatch; a backlog is drained over several reads.
FETCH_LIMIT = 500


def _iso(value) -> str | None:
    """Render a datetime as an ISO 8601 string, or None when there is none.

    Args:
        value: A datetime, or None.

    Returns:
        The ISO string, or None.
    """
    return value.isoformat() if value is not None else None


def _event_payload(event: RequestEvent, request: RequestRecord) -> dict:
    """Build the snake_case message one event sends to a live reader.

    It carries the event's own fields plus the request's current stage, status
    and the handful of columns a table row needs, so a reader can update a row
    in place without a second fetch. Outbox bodies and anything a timeline does
    not need are deliberately left out.

    Args:
        event: The timeline event row.
        request: The request the event is about (joined on request_id).

    Returns:
        The payload dict, JSON-serialisable.
    """
    return {
        "id": event.id,                                  # the stream position (SSE id)
        "request_id": event.request_id,                  # internal surrogate, for the detail fetch
        "kind": event.kind,                              # stored wire kind (carryover_payout, not the url form)
        "item_id": event.item_id,                        # the number people and links use
        "employee_id": event.employee_id,               # denormalised, for employee filtering
        "manager_id": event.manager_id,                 # denormalised, for manager filtering
        "type": event.type,                              # the event type (approved, rejected, ...)
        "channel": event.channel,                        # where it came from (email_link, sms, ...)
        "actor": event.actor,                            # who caused it, or None
        "detail": event.detail or {},                    # what a timeline line needs
        "created_at": _iso(event.created_at),            # when the event happened
        "stage": request.stage,                          # the request's stage in words, now
        "status": request.status,                        # pending / approved / rejected / ...
        "employee_name": request.employee_name,          # for the table row
        "leave_type": request.leave_type,                # leave only
        "start_date": _iso(request.start_date),          # first day / date worked
        "end_date": _iso(request.end_date),              # last day, leave range only
        "days": request.days,                            # cost in days
        "hours": request.hours,                          # overtime hours
        "request_option": request.request_option,        # carry_over / payout
        "decided_at": _iso(request.decided_at),          # when a manager decided, or None
        "request_created_at": _iso(request.created_at),  # when the request was made
    }


async def fetch_events(after_id: int, limit: int = FETCH_LIMIT) -> list[dict]:
    """Every event newer than an id, oldest first, as reader payloads.

    Shared by the bus (on each notify or poll) and the stream route (to replay
    what a reconnecting reader missed and to answer the ``/events`` catch-up).

    Args:
        after_id: Return events with id strictly greater than this.
        limit: The most events to return in one read.

    Returns:
        A list of payloads, ordered by event id ascending.
    """
    async with async_session() as session:
        rows = (await session.execute(
            select(RequestEvent, RequestRecord)
            .join(RequestRecord, RequestEvent.request_id == RequestRecord.id)
            .where(RequestEvent.id > after_id)
            .order_by(RequestEvent.id)
            .limit(limit)
        )).all()
    return [_event_payload(event, request) for event, request in rows]


async def _current_max_id() -> int:
    """The newest event id in the table, or 0 when there are none.

    Returns:
        The highest request_events.id, or 0.
    """
    async with async_session() as session:
        return int((await session.execute(select(func.max(RequestEvent.id)))).scalar() or 0)


class EventBus:
    """Fans request-timeline events out to in-process subscriber queues.

    One instance lives for the life of the process (``event_bus`` below). It
    starts in the app lifespan, runs one listener or poller task, and stops
    cleanly on shutdown.
    """

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()   # one queue per open reader
        self._last_id: int = 0                           # newest event already dispatched
        self._task: asyncio.Task | None = None           # the listener/poller task
        self._running = False

    def subscribe(self) -> asyncio.Queue:
        """Register a live reader and hand back the queue the bus pushes to.

        Returns:
            An unbounded asyncio.Queue the caller reads payloads from.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Drop a reader's queue when its connection closes.

        Args:
            queue: The queue returned by subscribe.
        """
        self._subscribers.discard(queue)

    async def start(self) -> None:
        """Begin listening (Postgres) or polling (SQLite) for new events.

        Sets the dispatch floor to the newest event now, so only events from
        here on are fanned out live; older history is replayed per connection.
        """
        if self._running:
            return
        self._running = True
        self._last_id = await _current_max_id()          # only dispatch what lands from now on
        self._task = asyncio.create_task(self._run())
        logger.info("Event bus started at event id %s (%s)", self._last_id, engine.dialect.name)

    async def stop(self) -> None:
        """Stop the listener/poller and drop every subscriber."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._subscribers.clear()

    async def _dispatch(self, payloads: list[dict]) -> None:
        """Push a batch of payloads to every subscriber and advance the floor.

        Args:
            payloads: Event payloads, ordered by id ascending.
        """
        for payload in payloads:
            self._last_id = max(self._last_id, int(payload["id"]))   # never dispatch the same id twice
            for queue in list(self._subscribers):        # a snapshot: a reader may unsubscribe mid-loop
                queue.put_nowait(payload)

    async def poll_once(self) -> int:
        """Read and dispatch every event newer than the floor, once.

        The poll loop calls this on a timer; tests call it directly to drive the
        SQLite path without waiting on the clock.

        Returns:
            How many events were dispatched.
        """
        payloads = await fetch_events(self._last_id)
        await self._dispatch(payloads)
        return len(payloads)

    async def _run(self) -> None:
        """Listen on Postgres, or poll everywhere else, until stopped."""
        if engine.dialect.name == "postgresql":
            await self._listen_postgres()
        else:
            await self._poll_loop()

    async def _poll_loop(self) -> None:
        """Dispatch new events every POLL_SECONDS (the no-NOTIFY path)."""
        while self._running:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - a failed read must not kill the poller
                logger.exception("Event bus poll failed")
            await asyncio.sleep(POLL_SECONDS)

    def _raw_dsn(self) -> str:
        """The plain libpq DSN for asyncpg, derived like app/database.py.

        app/database.py turns DATABASE_URL into a SQLAlchemy ``+asyncpg`` url;
        asyncpg.connect wants the bare ``postgresql://`` form, so this undoes
        that scheme rewrite.

        Returns:
            A ``postgresql://`` connection string.
        """
        from app.config import settings
        url = settings.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        elif url.startswith("postgresql+asyncpg://"):
            url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
        return url

    async def _listen_postgres(self) -> None:
        """Run LISTEN with backoff reconnect, replaying missed events each time.

        On every notify (and right after each connect) it dispatches every event
        newer than the floor, so a notify dropped during a connection blip is
        still delivered once the connection is back.
        """
        import asyncpg  # local import: only the Postgres path needs the driver

        backoff = RECONNECT_MIN_SECONDS
        while self._running:
            connection = None
            try:
                connection = await asyncpg.connect(self._raw_dsn())
                wake = asyncio.Event()                   # set by the notify callback

                def _on_notify(*_args):                  # asyncpg passes (conn, pid, channel, payload)
                    wake.set()                            # just wake the loop; it reads the table itself

                await connection.add_listener(NOTIFY_CHANNEL, _on_notify)
                await self.poll_once()                   # replay anything missed before this connect
                backoff = RECONNECT_MIN_SECONDS          # a clean connect resets the backoff
                while self._running:
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=POLL_SECONDS * 5)
                    except asyncio.TimeoutError:
                        pass                              # fall through and read anyway, as a safety net
                    wake.clear()
                    await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - any listener failure reconnects with backoff
                logger.exception("Event bus listener lost; reconnecting in %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
            finally:
                if connection is not None:
                    try:
                        await connection.close()
                    except Exception:  # noqa: BLE001 - closing a broken connection is best effort
                        pass


# The one bus for the process; started in main.py's lifespan.
event_bus = EventBus()


def sse_message(payload: dict) -> str:
    """Format one payload as a server-sent-events ``request_event`` message.

    Args:
        payload: An event payload from fetch_events / the bus.

    Returns:
        The SSE text block, ending in the blank line that terminates an event.
    """
    data = json.dumps(payload, default=str)              # default=str in case a stray value is not JSON
    return f"id: {payload['id']}\nevent: request_event\ndata: {data}\n\n"
