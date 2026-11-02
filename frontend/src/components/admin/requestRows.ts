import { RequestKind } from '../../constants/leaveColors';
import { hasAuditLog } from '../dataGridDefaults';
import { daysWaiting, requestKind, requestNote, requestWhat, requestWhen, requestWho } from '../../utils/requestText';

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

/** The three views of the one requests table. */
export type RequestView = 'pending' | 'stuck' | 'all';

/** Where a row came from, which decides the actions it offers. */
export type RowSource = 'pending' | 'stuck' | 'history';

/**
 * One row of the requests table. Pending, stuck and history rows come
 * from three endpoints with three shapes; each is flattened into this one so the
 * table, filters and side panel treat them alike. The original stays in `raw`
 * for the actions, which call the existing endpoints unchanged.
 */
export interface RequestRow {
  /** Unique across sources: "<source>-<request_type>-<id>". */
  key: string;
  source: RowSource;
  /** SharePoint item id. */
  id: string;
  /** 'leave', 'overtime' or 'carryover-payout'. */
  request_type: string;
  who: string;
  /** "Vacation", "Overtime", "Payout"... */
  what: string;
  kind: RequestKind | null;
  /** "Nov 2 to Nov 4", "2 days paid out"... */
  when: string;
  /** The employee's own words. */
  note: string;
  manager: string;
  /** Pending, Approved, Rejected, Refunded or Stuck. */
  status: string;
  /** Days since it was made, or null. */
  waiting: number | null;
  /** Stuck diagnostic codes. */
  issues: string[];
  /** The stuck diagnostic in a sentence. */
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
export function fromSharePoint(r: any, source: RowSource): RequestRow {
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
