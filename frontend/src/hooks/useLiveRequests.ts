import { Dispatch, SetStateAction, useEffect, useRef, useState } from 'react';
import {
  LiveEvent,
  LiveScope,
  eventToRow,
  fetchEventsAfter,
  openEventStream,
  requestTypeOf,
} from '../api/events';

/** What the live connection is doing right now, for a small status notice. */
export type LiveStatus = 'connecting' | 'live' | 'reconnecting' | 'polling';

// A dead stream shows no error but stops sending; after this long with nothing,
// including the server's heartbeats, we give up on it and poll instead.
const DEAD_MS = 45000;
// How often the polling fallback asks for events it missed.
const POLL_MS = 15000;
// While polling, how often to try the stream again (an admin ticket expires,
// so a stream the browser could not resume is reopened with a fresh one).
const RETRY_STREAM_MS = 60000;

/**
 * Merge one event's row into the loaded rows by (request_type, item_id).
 *
 * An existing row is updated in place, keeping fields the event does not carry
 * (managers, taken_from, Title); a new request is appended. The page's own sort
 * decides display order, so the stored order is left as it is.
 *
 * @param rows - The current rows.
 * @param ev - The event to apply.
 * @returns The next rows array, or the same one when nothing changed.
 */
function mergeEvent(rows: any[], ev: LiveEvent): any[] {
  const patch = eventToRow(ev);
  const type = requestTypeOf(ev.kind);
  const id = String(ev.item_id);
  const i = rows.findIndex((r) => r.request_type === type && String(r.id) === id);
  if (i === -1) {
    return [...rows, patch];                               // a request not loaded yet: add it
  }
  const next = rows.slice();
  next[i] = { ...rows[i], ...patch };                      // update in place, keep the rest
  return next;
}

/**
 * Keep a page's request rows live (#328-#331).
 *
 * Opens the server-sent-events stream for the scope and merges each event into
 * the rows, so an approval by email, text or another dashboard shows here
 * within a second or two. If the stream errors or goes silent it shows a
 * "Reconnecting" state and, when it cannot recover, falls back to polling the
 * catch-up endpoint every 15 seconds. All of it is torn down on unmount.
 *
 * @param options.scope - Which feed to read ('me', 'team' or 'admin').
 * @param options.setRows - The page's rows setter; merged into in place.
 * @param options.enabled - Open the connection only once the page is ready.
 * @param options.onEvent - Called for every event, so a page can also refresh
 *   its pending queue on a decision.
 * @returns The current connection status, for a small notice.
 */
export function useLiveRequests(options: {
  scope: LiveScope;
  setRows: Dispatch<SetStateAction<any[]>>;
  enabled?: boolean;
  onEvent?: (ev: LiveEvent) => void;
}): { status: LiveStatus } {
  const { scope, setRows, enabled = true, onEvent } = options;
  const [status, setStatus] = useState<LiveStatus>('connecting');

  // Kept in refs so the long-lived stream and timers never close over stale
  // state, and so changing handlers does not reopen the connection.
  const setRowsRef = useRef(setRows);
  const onEventRef = useRef(onEvent);
  setRowsRef.current = setRows;
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!enabled) return;
    let closed = false;                                    // set on unmount, stops every retry
    let source: EventSource | null = null;
    let deadTimer: ReturnType<typeof setTimeout> | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let lastId: number | null = null;                      // resume point; null = start from now
    // Ids already applied on this connection. The stream and the catch-up both
    // re-scan a window below the high-water mark so an event that committed out
    // of order is not skipped (finding 9); that means the same id can arrive
    // twice, so apply each id once here. Bounded so it cannot grow without end.
    const seen = new Set<number>();
    const SEEN_CAP = 2000;

    /** Apply one event once: skip a repeat id, advance the resume point, merge, tell the page. */
    const apply = (ev: LiveEvent) => {
      if (seen.has(ev.id)) return;                         // already applied (a re-scan re-sent it)
      seen.add(ev.id);
      if (seen.size > SEEN_CAP) {                          // forget the oldest ids on a long connection
        const floor = Math.max(...seen) - SEEN_CAP;
        for (const id of seen) if (id <= floor) seen.delete(id);
      }
      lastId = Math.max(lastId ?? 0, ev.id);
      setRowsRef.current((rows) => mergeEvent(rows, ev));
      onEventRef.current?.(ev);
    };

    /** Restart the dead-stream timer; a silent stream trips it and we poll. */
    const armDead = () => {
      if (deadTimer) clearTimeout(deadTimer);
      deadTimer = setTimeout(() => startPolling(), DEAD_MS);
    };

    const clearDead = () => {
      if (deadTimer) { clearTimeout(deadTimer); deadTimer = null; }
    };

    /** Stop the polling fallback once the stream is back. */
    const stopPolling = () => {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    };

    /** Open the stream and wire its events; the browser auto-reconnects drops. */
    const startStream = async () => {
      if (closed) return;
      const opened = await openEventStream(scope, lastId);
      if (closed) { opened?.close(); return; }             // unmounted while the ticket was fetched
      source = opened;
      if (!source) { startPolling(); return; }             // not signed in for this scope
      setStatus('connecting');
      armDead();                                           // arm now: a stream stuck connecting still trips the fallback
      source.onopen = () => { if (!closed) { stopPolling(); setStatus('live'); armDead(); } };
      // The server's first message says where "now" is, so a later resume starts there.
      source.addEventListener('ready', (e) => {
        const id = Number((e as MessageEvent).lastEventId);
        if (!Number.isNaN(id)) lastId = Math.max(lastId ?? 0, id);
      });
      source.addEventListener('request_event', (e) => {
        if (closed) return;
        armDead();                                         // a message means the stream is alive
        try {
          apply(JSON.parse((e as MessageEvent).data));
        } catch {
          /* a malformed frame is skipped; the next good one still lands */
        }
      });
      source.onerror = () => {
        if (closed) return;
        // readyState CLOSED (2) means the browser gave up; poll instead.
        if (source && source.readyState === 2) {
          startPolling();
        } else {
          setStatus('reconnecting');                       // CONNECTING: the browser is retrying
        }
      };
    };

    /** One catch-up read, applying every event the poll returns. */
    const pollOnce = async () => {
      try {
        const { events, lastId: next } = await fetchEventsAfter(scope, lastId);
        if (closed) return;
        events.forEach(apply);
        lastId = Math.max(lastId ?? 0, next);              // also skips events this viewer cannot see
      } catch {
        /* a failed poll is retried on the next tick */
      }
    };

    /** (Re)arm the stream retry while polling, so a failed reopen is tried again. */
    const armRetry = () => {
      if (closed) return;
      if (retryTimer) clearTimeout(retryTimer);            // one retry pending at a time
      // Keep trying the stream; polling is the fallback, not the goal.
      retryTimer = setTimeout(() => { retryTimer = null; void startStream(); }, RETRY_STREAM_MS);
    };

    /** Give up on the stream and read the catch-up on a timer instead. */
    const startPolling = () => {
      if (closed) return;
      if (source) { source.close(); source = null; }       // drop any half-open stream
      clearDead();
      // Already polling: a retry's reopen failed (the retry fired and cleared
      // itself), so re-arm it and keep polling rather than getting stuck here.
      if (pollTimer) { armRetry(); return; }
      setStatus('polling');
      void pollOnce();
      pollTimer = setInterval(() => void pollOnce(), POLL_MS);
      armRetry();
    };

    void startStream();

    return () => {
      closed = true;
      if (source) source.close();
      clearDead();
      if (pollTimer) clearInterval(pollTimer);
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [scope, enabled]);

  return { status };
}
