import { RequestKind, kindOf } from '../../constants/leaveColors';
import { getDescription } from '../dataGridDefaults';
import { daysWaiting } from '../../utils/requestText';
import { formatDay, formatDayRange } from '../../utils/dates';

/**
 * The one requests table spans the three SharePoint lists (Leave Requests,
 * Overtime Requests Data = make-up, CarryOver_Payout_Data). This module turns
 * each raw request item the admin endpoints return into one flat row the
 * table, calendar, filters, stats and export all read, and holds the column
 * sets, their drop priority and the plain-language helpers. It is pure (no
 * JSX); the table component renders each cell from these.
 */

/** Which of the three lists a row belongs to. */
export type Src = 'leave' | 'mu' | 'co';

/** Where a row was read from, which decides the actions it offers. */
export type RowSource = 'pending' | 'stuck' | 'history';

/** One request, flattened across the three lists. */
export interface Row {
  key: string;
  id: string;
  src: Src;
  request_type: string;              // 'leave' | 'overtime' | 'carryover-payout'
  source: RowSource;
  kind: RequestKind | null;
  kindLabel: string;
  person: string;
  dept: string;
  loc: string;
  manager: string;                   // first/primary manager
  managers: string[];                // all managers named
  status: string;                    // Pending/Approved/Rejected/Refunded/Stuck
  stage: string;                     // the live stage words (Postgres mode), or ''
  created: string;                   // ISO date (date part)
  approved: string;                  // ISO date, or ''
  start: string;                     // leave start, or ''
  end: string;                       // leave end, or ''
  date: string;                      // overtime date, or ''
  days: number | null;
  hours: number | null;
  amountDays: number;                // days, or hours/8 for make-up, for sort/stats
  title: string;                     // the person's own words
  submittedBy: string;
  sysState: string;                  // SystemState (carry-over), or a derived state
  newBalance: string;                // carry-over NewBalance, raw
  waiting: number | null;            // days since created, or null for decided
  diagnostics: string[];             // stuck diagnostic codes
  auditLog: string;                  // BalanceAuditLog JSON, or ''
  raw: any;
}

/** 'leave'/'overtime'/'carryover-payout' to the list key. */
const SRC_OF: Record<string, Src> = { leave: 'leave', overtime: 'mu', 'carryover-payout': 'co' };

/** The short label for a request's kind, as a person says it. */
export function kindLabelOf(r: any): string {
  if (r.request_type === 'overtime') return 'Make-up';
  if (r.request_type === 'carryover-payout') return r.TypeofRequest === 'Payout' ? 'Payout' : 'Carry Over';
  const t = r.LeaveType || 'Leave';
  return t === 'Sick or Personal Day' ? 'Sick or personal' : t === 'Half Day or Partial Day Off' ? 'Part day' : t;
}

/** Managers as a list, from the backend's "A, B" string. */
function managerList(managers: unknown): string[] {
  if (Array.isArray(managers)) return managers.filter(Boolean).map(String);
  if (typeof managers === 'string') return managers.split(',').map((m) => m.trim()).filter(Boolean);
  return [];
}

/**
 * Flatten one raw request item into a table row.
 *
 * @param raw - The item as /admin/requests, /admin/pending or /admin/stuck-requests returns it.
 * @param source - 'pending', 'stuck' or 'history'.
 * @returns The row.
 */
export function toRow(raw: any, source: RowSource): Row {
  // Stuck items are always leave and the endpoint omits request_type.
  const item = source === 'stuck' && !raw.request_type ? { ...raw, request_type: 'leave' } : raw;
  const type: string = item.request_type || 'leave';
  const src = SRC_OF[type] || 'leave';
  const days = item.Days != null && item.Days !== '' ? Number(item.Days) : null;
  const hours = item.Hours != null && item.Hours !== '' ? Number(item.Hours) : null;
  const managers = managerList(item.managers);
  const status = source === 'stuck' ? 'Stuck' : (item.Status || 'Pending');
  const created = (item.Created || '').slice(0, 10);
  return {
    key: `${source}-${type}-${item.id}`,
    id: String(item.id),
    src,
    request_type: type,
    source,
    kind: kindOf(type, type === 'carryover-payout' ? item.TypeofRequest : item.LeaveType),
    kindLabel: kindLabelOf(item),
    person: item.employee_name || (item.Title || '').split(' /// ')[0] || 'Unknown',
    dept: item.StaffDepartment || item.Department || '',
    loc: item.StaffLocation || item.Location || '',
    manager: managers[0] || '',
    managers,
    status,
    stage: item.stage || '',               // present in Postgres mode, blank in SharePoint mode
    created,
    approved: (item.ApprovedDate || '').slice(0, 10),
    start: (item.StartDate || '').slice(0, 10),
    end: (item.EndDate || '').slice(0, 10),
    date: src === 'mu' ? (item.StartDate || '').slice(0, 10) : '',
    days,
    hours,
    amountDays: src === 'mu' ? (hours || 0) / 8 : (days || 0),
    title: getDescription(item),
    submittedBy: item.employee_name || item.SubmitterName || '',
    sysState: item.SystemState || (status === 'Pending' ? 'Waiting on manager' : 'Processed'),
    newBalance: item.NewBalance || '',
    waiting: source === 'history' ? null : daysWaiting(item),
    diagnostics: item.diagnostics || [],
    auditLog: typeof item.BalanceAuditLog === 'string' ? item.BalanceAuditLog : '',
    raw: item,
  };
}

/** Each list, with the note shown under the source switch. */
export const SRC_META: Record<Src | 'all', { label: string; sub: string; note: string }> = {
  all: { label: 'All lists', sub: '', note: 'Leave Requests, Overtime Requests Data and CarryOver_Payout_Data together' },
  leave: { label: 'Leave', sub: '', note: 'SharePoint list: Leave Requests' },
  mu: { label: 'Make-up', sub: '', note: 'SharePoint list: Overtime Requests Data. Hours become days at 8 hours a day.' },
  co: { label: 'Carry-over and payout', sub: '', note: 'SharePoint list: CarryOver_Payout_Data' },
};

/** A column's display settings; the cell itself is rendered by the table. */
export interface ColumnDef {
  label: string;
  numeric?: boolean;
  wrap?: boolean;
  width: number;                     // assumed width for the no-sideways-scroll fit
  noSort?: boolean;
}

/** Every column that can appear, by key. */
export const COLUMNS: Record<string, ColumnDef> = {
  id: { label: 'ID', numeric: true, width: 70 },
  kind: { label: 'Type', width: 120 },
  person: { label: 'Person', wrap: true, width: 180 },
  what: { label: 'Details', wrap: true, width: 220 },
  when: { label: 'When', wrap: true, width: 150 },
  amount: { label: 'Amount', numeric: true, width: 90 },
  // Which balances a request's days came from (approved) or would (pending); chips wrap, so it narrows before dropping.
  taken: { label: 'Taken from', wrap: true, width: 200, noSort: true },
  status: { label: 'Status', wrap: true, width: 140 },
  stage: { label: 'Stage', wrap: true, width: 170 },
  mgr: { label: 'Manager', width: 150 },
  created: { label: 'Created', width: 110 },
  approved: { label: 'Approved', width: 110 },
  start: { label: 'Start', width: 110 },
  end: { label: 'End', width: 110 },
  days: { label: 'Days', numeric: true, width: 80 },
  date: { label: 'Date', width: 110 },
  hours: { label: 'Hours', numeric: true, width: 80 },
  hdays: { label: 'As days', numeric: true, width: 90 },
  newbal: { label: 'New balance', width: 180 },
  sysstate: { label: 'System state', width: 140 },
  quick: { label: 'Actions', width: 120, noSort: true },
};

/** The columns each list shows, in display order. All lists shares one set. */
export const COLUMN_SETS: Record<Src | 'all', string[]> = {
  all: ['id', 'kind', 'person', 'what', 'when', 'amount', 'taken', 'status', 'stage', 'mgr', 'created', 'quick'],
  leave: ['id', 'kind', 'person', 'what', 'start', 'end', 'days', 'taken', 'status', 'stage', 'mgr', 'approved', 'created', 'quick'],
  mu: ['id', 'person', 'what', 'date', 'hours', 'hdays', 'taken', 'status', 'stage', 'mgr', 'approved', 'created', 'quick'],
  co: ['id', 'kind', 'person', 'days', 'taken', 'status', 'stage', 'mgr', 'newbal', 'sysstate', 'approved', 'created', 'quick'],
};

// Most important first: when space runs out the LAST of these present in the
// set drops first. Person, status, type, amount and the actions are kept; ids,
// dates and system fields step aside (their values stay in the opened row).
const PRIORITY = [
  'person', 'status', 'quick', 'kind', 'amount', 'days', 'hours', 'when', 'taken', 'stage',
  'start', 'date', 'what', 'end', 'mgr', 'newbal', 'created', 'approved', 'hdays', 'sysstate', 'id',
];

/**
 * A list's columns ordered most-important-first, for the fit hook.
 *
 * @param src - 'all' or one list.
 * @returns The set's keys sorted by drop priority.
 */
export function priorityOrder(src: Src | 'all'): string[] {
  return [...COLUMN_SETS[src]].sort((a, b) => PRIORITY.indexOf(a) - PRIORITY.indexOf(b));
}

/** The assumed width of each column, for the fit hook. */
export const COLUMN_WIDTHS: Record<string, number> = Object.fromEntries(
  Object.entries(COLUMNS).map(([k, c]) => [k, c.width]),
);

/** "Nov 2 to Nov 4, 2026", "Nov 3, 2026", or '' for a carry-over (no dates). */
export function whenText(r: Row): string {
  if (r.src === 'mu') return formatDay(r.date);          // overtime: one calendar day
  if (r.src === 'co') return '';                          // carry-over/payout has no dates
  if (!r.start) return '';                                // leave with no start yet
  return formatDayRange(r.start, r.end);                  // leave: full day range, one date when same
}

/** "3 d" for leave/carry-over, "5 h" for make-up, '' when unknown. */
export function amountText(r: Row): string {
  if (r.src === 'mu') return r.hours == null ? '' : `${num(r.hours)} h`;
  return r.days == null ? '' : `${num(r.days)} d`;
}

/** The date a row is placed on in a range filter or the calendar. */
export function refDate(r: Row): string {
  if (r.src === 'mu') return r.date;
  if (r.src === 'co') return r.created;
  return r.start || r.created;
}

/** A number trimmed to at most three decimals, as text. */
export function num(n: number, dp?: number): string {
  const r = Math.round(n * 1000) / 1000;
  return dp != null ? r.toFixed(dp) : String(r);
}

/** The value used to sort or search one column of a row. */
export function columnValue(r: Row, key: string): string | number {
  switch (key) {
    case 'id': return Number(r.id);
    case 'kind': return r.kindLabel;
    case 'person': return r.person;
    case 'what': return r.title;
    case 'when': return refDate(r);
    case 'amount': return r.amountDays || 0;
    case 'status': return r.status;
    case 'stage': return r.stage;
    case 'mgr': return r.manager;
    case 'created': return r.created;
    case 'approved': return r.approved;
    case 'start': return r.start;
    case 'end': return r.end;
    case 'days': return r.days || 0;
    case 'date': return r.date;
    case 'hours': return r.hours || 0;
    case 'hdays': return (r.hours || 0) / 8;
    case 'newbal': return r.newBalance;
    case 'sysstate': return r.sysState;
    default: return '';
  }
}

/** Everything a search box matches a row against, lower-cased. */
export function searchText(r: Row): string {
  return [r.id, r.person, r.manager, r.title, r.kindLabel, r.dept, r.loc, r.submittedBy]
    .join(' ').toLowerCase();
}

/** One filter group in the filter drawer. */
export interface FilterDef {
  key: string;
  label: string;
  options: string[];
  test: (r: Row, value: string) => boolean;
}

/** Unique, sorted, non-empty values. */
function uniq(values: string[]): string[] {
  return Array.from(new Set(values)).filter(Boolean).sort();
}

/**
 * The filter groups for the rows in scope (the chosen list's rows).
 *
 * @param rows - Rows of the current list (before the view/search filters).
 * @returns Type, status, manager, department and location groups.
 */
export function filterDefs(rows: Row[]): FilterDef[] {
  return [
    { key: 'kind', label: 'Type', options: uniq(rows.map((r) => r.kindLabel)), test: (r, v) => r.kindLabel === v },
    { key: 'status', label: 'Status', options: ['Pending', 'Approved', 'Rejected', 'Refunded'], test: (r, v) => r.status === v },
    { key: 'mgr', label: 'Manager', options: uniq(rows.flatMap((r) => r.managers)), test: (r, v) => r.managers.includes(v) },
    { key: 'dept', label: 'Department', options: uniq(rows.map((r) => r.dept)), test: (r, v) => r.dept === v },
    { key: 'loc', label: 'Location', options: uniq(rows.map((r) => r.loc)), test: (r, v) => r.loc === v },
  ];
}
