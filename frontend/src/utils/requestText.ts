import { getDescription } from '../components/dataGridDefaults';
import { kindOf, RequestKind } from '../constants/leaveColors';

/**
 * Plain-language helpers for a request row as the dashboard endpoints return
 * it: SharePoint fields (StartDate, LeaveType, Days, Created...) plus
 * request_type and, where the backend resolved it, employee_name.
 */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * "Nov 3" from "2026-11-03" (or a full ISO timestamp). No timezone shift: the
 * date part is read as written.
 *
 * @param iso - A date or timestamp string.
 * @returns The short date, or '' when missing.
 */
export function shortDate(iso?: string | null): string {
  if (!iso) return '';
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);   // date part only
  if (!y || !m || !d) return '';
  return `${MONTHS[m - 1]} ${d}`;
}

/**
 * Who the request is for.
 *
 * @param r - The request row.
 * @returns The person's name, or 'Unknown'.
 */
export function requestWho(r: any): string {
  // Leave Titles carry "<name> /// <description>"; the resolved name wins when present.
  return r.employee_name || (r.Title || '').split(' /// ')[0] || r.SubmitterName || 'Unknown';
}

/**
 * The kind of request ("Vacation", "Overtime", "Payout"...), as a person says it.
 *
 * @param r - The request row.
 * @returns A short label.
 */
export function requestWhat(r: any): string {
  if (r.request_type === 'overtime') return 'Overtime';
  if (r.request_type === 'carryover-payout') return r.TypeofRequest === 'Payout' ? 'Payout' : 'Carry Over';
  const t = r.LeaveType || 'Leave';
  // Shorter names for the two long leave types.
  return t === 'Sick or Personal Day' ? 'Sick' : t === 'Half Day or Partial Day Off' ? 'Part day' : t;
}

/**
 * The kind used for colours.
 *
 * @param r - The request row.
 * @returns The kind, or null.
 */
export function requestKind(r: any): RequestKind | null {
  return kindOf(r.request_type, r.request_type === 'carryover-payout' ? r.TypeofRequest : r.LeaveType);
}

/**
 * When it is for: "Nov 2 to Nov 4", "Nov 3", or the amount for a carry-over.
 *
 * @param r - The request row.
 * @returns A short phrase.
 */
export function requestWhen(r: any): string {
  if (r.request_type === 'carryover-payout') {
    const d = Number(r.Days || 0);
    return `${d} day${d === 1 ? '' : 's'} ${r.TypeofRequest === 'Payout' ? 'paid out' : 'into next year'}`;
  }
  const a = shortDate(r.StartDate), b = shortDate(r.EndDate);
  return b && b !== a ? `${a} to ${b}` : a;
}

/**
 * How much it is: days for leave and carry-over, hours for overtime.
 *
 * @param r - The request row.
 * @returns "3 days", "4 hours", or ''.
 */
export function requestAmount(r: any): string {
  if (r.Hours != null && r.request_type === 'overtime') return `${r.Hours} hour${Number(r.Hours) === 1 ? '' : 's'}`;
  if (r.Days != null && r.Days !== '') return `${r.Days} day${Number(r.Days) === 1 ? '' : 's'}`;
  return '';
}

/**
 * Whole days since the request was created.
 *
 * @param r - The request row (Created, an ISO timestamp).
 * @param now - The time to measure to (defaults to now).
 * @returns Days, or null when Created is missing.
 */
export function daysWaiting(r: any, now: Date = new Date()): number | null {
  if (!r.Created) return null;
  const t = Date.parse(r.Created);
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.floor((now.getTime() - t) / 86400000));
}

/**
 * "asked today", "asked yesterday", "asked 12 days ago".
 *
 * @param r - The request row.
 * @returns The phrase, or '' when unknown.
 */
export function askedAgo(r: any): string {
  const d = daysWaiting(r);
  if (d === null) return '';
  return d === 0 ? 'asked today' : d === 1 ? 'asked yesterday' : `asked ${d} days ago`;
}

/**
 * The employee's own words on the request, if any. Overtime and carry-over
 * Titles are often just the person's name; that is not a description.
 *
 * @param r - The request row.
 * @returns The description, or ''.
 */
export function requestNote(r: any): string {
  const d = getDescription(r);
  return d === requestWho(r) ? '' : d;
}
