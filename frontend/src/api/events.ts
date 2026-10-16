/**
 * The browser side of the live request feed (#328-#332).
 *
 * The backend streams a request's timeline events over server-sent events
 * (``/events/stream``) and answers a catch-up poll (``/events?after=``). This
 * module opens that stream with the page's own sign-in, turns an event into the
 * table-row shape the dashboards already read, fetches the catch-up, loads one
 * request's timeline, and checks the running build version. The hook in
 * ``../hooks/useLiveRequests`` drives it; this file holds no React.
 */
import api from './client';
import { adminSessionToken } from './adminSession';
import { loadSession } from './intake';

const API_URL = import.meta.env.VITE_API_URL || '';

/** Which feed a page reads: its own, its team's, or everything (admin). */
export type LiveScope = 'me' | 'team' | 'admin';

/** One request-timeline event, exactly as the backend sends it (snake_case). */
export interface LiveEvent {
  id: number;
  request_id: number;
  kind: string;                 // stored wire kind: leave / overtime / carryover_payout
  item_id: number;
  employee_id: string | null;
  manager_id: string | null;
  type: string;                 // approved / rejected / submitted / ...
  channel: string;
  actor: string | null;
  detail: Record<string, unknown>;
  created_at: string | null;
  stage: string | null;         // the request's stage in words, now
  status: string | null;        // pending / approved / rejected / ...
  employee_name: string | null;
  leave_type: string | null;
  start_date: string | null;
  end_date: string | null;
  days: number | null;
  hours: number | null;
  request_option: string | null;
  decided_at: string | null;
  request_created_at: string | null;
}

/** One line of a request's timeline, as the timeline endpoints return it. */
export interface TimelineEvent {
  id: number;
  type: string;
  channel: string;
  actor: string | null;
  at: string | null;            // ISO instant
  line: string;                 // plain words, e.g. "Approved by email by Dave"
}

/** One field change on a request (admin field history, #332). */
export interface TimelineChange {
  field: string;
  old: unknown;
  new: unknown;
  actor: string | null;
  channel: string | null;
  at: string | null;
}

/** A request's full timeline: event lines, plus field history for admins. */
export interface RequestTimeline {
  events: TimelineEvent[];
  changes: TimelineChange[];
}

/**
 * The url-form request type for a stored kind, so an event matches a table row.
 *
 * @param kind - The stored kind (``carryover_payout`` uses an underscore).
 * @returns The url form the tables use (``carryover-payout`` uses a hyphen).
 */
export function requestTypeOf(kind: string): string {
  return kind === 'carryover_payout' ? 'carryover-payout' : kind;
}

/**
 * A status stored lower case, shown title case to match the status chips.
 *
 * @param status - The stored status, or null.
 * @returns "Pending", "Approved", ... or "" when there is none.
 */
export function statusLabel(status: string | null): string {
  if (!status) return '';
  return status.charAt(0).toUpperCase() + status.slice(1);
}

/**
 * The sign-in query parameters for a scope, read fresh each time.
 *
 * @param scope - The feed scope.
 * @returns The parameters, or null when nobody is signed in for that scope.
 */
function authParams(scope: LiveScope): Record<string, string> | null {
  if (scope === 'admin') {
    const token = adminSessionToken();
    return token ? { admin_session: token } : null;
  }
  const session = loadSession();
  if (!session) return null;
  const { token, role, uid, exp } = session;
  return { token, role, uid, exp };
}

/**
 * Turn an event into the partial table row the dashboards read (SharePoint
 * field names), so a hook can merge it into a loaded row in place.
 *
 * @param ev - The event from the stream or the catch-up.
 * @returns A row-shaped object keyed by request_type and id.
 */
export function eventToRow(ev: LiveEvent): Record<string, unknown> {
  const option =
    ev.request_option === 'payout' ? 'Payout' : ev.request_option === 'carry_over' ? 'Carry Over' : '';
  return {
    request_type: requestTypeOf(ev.kind),
    id: String(ev.item_id),
    stage: ev.stage || '',
    Status: statusLabel(ev.status),
    employee_name: ev.employee_name || '',
    LeaveType: ev.leave_type || '',
    StartDate: ev.start_date || '',
    EndDate: ev.end_date || '',
    Days: ev.days ?? null,
    Hours: ev.hours ?? null,
    TypeofRequest: option,
    ApprovedDate: ev.decided_at || '',
    Created: ev.request_created_at || '',
  };
}

/**
 * The server-sent-events URL for a scope, resuming after an event id.
 *
 * @param scope - The feed scope.
 * @param afterId - Replay events newer than this (0 for from-now).
 * @returns The absolute stream URL, or null when the scope is not signed in.
 */
export function streamUrl(scope: LiveScope, afterId: number): string | null {
  const params = authParams(scope);
  if (!params) return null;
  const query = new URLSearchParams({ ...params, after: String(afterId) });
  return `${API_URL}/api/dashboard/events/stream?${query.toString()}`;
}

/**
 * Open the live event stream, or return null when there is no sign-in to use.
 *
 * The browser reconnects a dropped EventSource on its own and resends the last
 * event id; this url also carries ``after`` so a freshly made EventSource picks
 * up from the same place.
 *
 * @param scope - The feed scope.
 * @param afterId - Replay events newer than this.
 * @returns An open EventSource, or null when the scope is not signed in.
 */
export function openEventStream(scope: LiveScope, afterId: number): EventSource | null {
  const url = streamUrl(scope, afterId);
  return url ? new EventSource(url) : null;
}

/**
 * The polling fallback: every event the viewer may see after an id.
 *
 * @param scope - The feed scope.
 * @param afterId - Return events newer than this.
 * @returns The events, newest-applied-last, and the running build version.
 */
export async function fetchEventsAfter(
  scope: LiveScope,
  afterId: number,
): Promise<{ events: LiveEvent[]; version: string }> {
  const params = scope === 'admin' ? { admin_session: adminSessionToken() } : undefined;
  const resp = await api.get('/events', { params: { ...(params || {}), after: afterId } });
  return { events: resp.data.events || [], version: resp.data.version || '' };
}

/**
 * One request's timeline, from the endpoint that matches the scope.
 *
 * @param scope - 'me', 'team' or 'admin'; admins also get the field history.
 * @param requestType - The url-form kind.
 * @param itemId - The item id (the number people see).
 * @returns The event lines and (for admins) the field changes.
 */
export async function getRequestTimeline(
  scope: LiveScope,
  requestType: string,
  itemId: string,
): Promise<RequestTimeline> {
  const resp = await api.get(`/${scope}/requests/${requestType}/${itemId}/timeline`);
  return { events: resp.data.events || [], changes: resp.data.changes || [] };
}

/**
 * The running backend build version (#331), for the auto-update check.
 *
 * @returns The version string, or "" when the call fails.
 */
export async function fetchVersion(): Promise<string> {
  try {
    const resp = await api.get('/version');
    return resp.data.version || '';
  } catch {
    return '';
  }
}
