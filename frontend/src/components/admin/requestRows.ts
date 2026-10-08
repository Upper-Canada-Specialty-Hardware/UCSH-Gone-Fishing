import { RequestKind } from '../../constants/leaveColors';
import { hasAuditLog } from '../dataGridDefaults';
import { daysWaiting, requestKind, requestNote, requestWhat, requestWhen, requestWho } from '../../utils/requestText';

/** One held request, as /admin/held-requests returns it. */
export interface HeldRow {
  id: number;
  email: string;
  name: string;
  location: string;
  supervisor_name: string;
  request_type: string;
  summary: string;
  status: string;
  created_at: string | null;
  supervisor_reminded_at: string | null;
  admins_notified_at: string | null;
  sp_item_id: string | null;
  last_error: string | null;
}

/** Held statuses in plain words. waiting_site: on staff, not on the SharePoint site yet; retried every hour. */
export const HELD_STATUS_LABEL: Record<string, string> = {
  held: 'Not on staff', waiting_site: 'Waiting for site access', releasing: 'Sending',
  failed: 'Failed', released: 'Sent', cancelled: 'Cancelled',
};

/** Held statuses that still need someone; matches the backend's OPEN_STATUSES. */
export const OPEN_HELD = ['held', 'waiting_site', 'failed'];

/** What each stuck-request diagnostic code means, and how serious it is. */
export const DIAGNOSTIC_LABELS: Record<string, { label: string; color: 'error' | 'warning' }> = {
  missing_dates: { label: 'Missing dates', color: 'error' },
  missing_employee: { label: 'Employee not found', color: 'error' },
  missing_all_managers: { label: 'No managers (Staff Directory)', color: 'error' },
  missing_days: { label: 'Days not calculated', color: 'warning' },
  missing_manager_lookup: { label: 'Manager lookup failed', color: 'warning' },
  approval_email_pending: { label: 'Approval email never sent', color: 'warning' },
  // Not a mechanical gap: the request is complete, but approved leave already
  // holds those dates, so approving it is refused. Reprocessing will not help;
  // it needs a decision from the manager.
  blocked_by_overlap: { label: 'Blocked by approved leave', color: 'error' },
};

/** The four views of the one requests table. */
export type RequestView = 'pending' | 'held' | 'stuck' | 'all';

/** Where a row came from, which decides the actions it offers. */
export type RowSource = 'pending' | 'held' | 'stuck' | 'history';

/**
 * One row of the requests table. Pending, held, stuck and history rows come
 * from four endpoints with four shapes; each is flattened into this one so the
 * table, filters and side panel treat them alike. The original stays in `raw`
 * for the actions, which call the existing endpoints unchanged.
 */
export interface RequestRow {
  /** Unique across sources: "<source>-<request_type>-<id>". */
  key: string;
  source: RowSource;
  /** SharePoint item id (or the held request's own id). */
  id: string;
  /** 'leave', 'overtime' or 'carryover-payout'. */
  request_type: string;
  who: string;
  /** "Vacation", "Overtime", "Payout"... */
  what: string;
  kind: RequestKind | null;
  /** "Nov 2 to Nov 4", "2 days paid out"... */
  when: string;
  /** The employee's own words, or the held request's summary. */
  note: string;
  manager: string;
  /** Pending, Approved, Rejected, Refunded, Stuck, or a held status in words. */
  status: string;
  /** Days since it was made, or null. */
  waiting: number | null;
  /** Stuck diagnostic codes. */
  issues: string[];
  /** The stuck diagnostic in a sentence, or a held release error. */
  detail: string;
  /** Whether the audit trail can be shown. */
  audit: boolean;
  raw: any;
}

/** The type filter's options and how each matches a row. */
export const TYPE_FILTERS: Record<string, (r: RequestRow) => boolean> = {
  'Any type': () => true,
  Leave: (r) => r.request_type === 'leave',
  Overtime: (r) => r.request_type === 'overtime',
  'Carry Over': (r) => r.kind === 'carry',
  Payout: (r) => r.kind === 'payout',
};

/**
 * A pending, stuck or history row (all SharePoint request items).
 *
 * @param r - The item as the admin endpoint returns it.
 * @param source - Which endpoint it came from.
 * @returns The table row.
 */
export function fromSharePoint(r: any, source: Exclude<RowSource, 'held'>): RequestRow {
  // Stuck items are always leave, and the endpoint leaves request_type off.
  const item = source === 'stuck' ? { ...r, request_type: 'leave' } : r;
  return {
    key: `${source}-${item.request_type}-${item.id}`,
    source,
    id: String(item.id),
    request_type: item.request_type,
    who: requestWho(item),
    what: requestWhat(item),
    kind: requestKind(item),
    when: requestWhen(item),
    note: requestNote(item),
    manager: typeof item.managers === 'string' ? item.managers : (item.managers || []).join(', '),
    status: source === 'stuck' ? 'Stuck' : item.Status || 'Pending',
    // Waiting only means something while nobody has decided.
    waiting: source === 'history' ? null : daysWaiting(item),
    issues: item.diagnostics || [],
    detail: item.diagnostic_detail || '',
    audit: hasAuditLog(item),
    raw: item,
  };
}

/** Request type to a short label, for held rows (they carry no leave type). */
const HELD_WHAT: Record<string, string> = { leave: 'Leave', overtime: 'Overtime', 'carryover-payout': 'Carry Over / Payout' };

/**
 * A held request (someone not on staff yet).
 *
 * @param h - The row from /admin/held-requests.
 * @returns The table row.
 */
export function fromHeld(h: HeldRow): RequestRow {
  return {
    key: `held-${h.request_type}-${h.id}`,
    source: 'held',
    id: String(h.id),
    request_type: h.request_type,
    who: h.name,
    what: HELD_WHAT[h.request_type] || h.request_type,
    // Leave colour is unknown until it reaches SharePoint; overtime is known.
    kind: h.request_type === 'overtime' ? 'overtime' : null,
    when: '',
    note: h.summary,
    manager: h.supervisor_name,
    status: HELD_STATUS_LABEL[h.status] ?? h.status,
    waiting: daysWaiting({ Created: h.created_at }),
    issues: [],
    detail: h.last_error || '',
    audit: false,
    raw: h,
  };
}
