import { RequestKind } from '../../constants/leaveColors';
import { addDays, dayKey, daysBetween, isWeekend, mondayOf, overlaps, parseDay, today } from '../../utils/dates';
import { requestKind, requestWhat, requestWho, shortDate } from '../../utils/requestText';

/**
 * Pure calculations behind the manager dashboard's tiles, charts and notes.
 * Everything here is worked out from data the team endpoints already return
 * (/team/members, /team/pending, /team/requests, /team/calendar); nothing is
 * stored or estimated.
 */

/** One stretch of time away, approved or still asked. */
export interface Absence {
  key: string;
  who: string;
  start: Date;
  end: Date;
  /** "Vacation", "Sick", "Part day"... */
  label: string;
  kind: RequestKind | null;
  /** Asked but not approved yet. */
  pending: boolean;
}

/**
 * Approved leave (from /team/calendar) and waiting leave (from /team/pending)
 * as one list of absences. Overtime and carry-over are not time away.
 *
 * @param events - /team/calendar events: {id, employee, start, end, leave_type}.
 * @param pending - /team/pending items.
 * @returns Every absence with readable dates.
 */
export function absencesFrom(events: any[], pending: any[]): Absence[] {
  const out: Absence[] = [];
  for (const e of events) {
    const start = parseDay(e.start), end = parseDay(e.end) || start;
    if (!start || !end) continue;                        // an event without dates cannot be placed
    const row = { request_type: 'leave', LeaveType: e.leave_type };
    out.push({ key: `a-${e.id}`, who: e.employee, start, end, label: requestWhat(row), kind: requestKind(row), pending: false });
  }
  for (const p of pending) {
    if (p.request_type !== 'leave') continue;
    const start = parseDay(p.StartDate), end = parseDay(p.EndDate) || start;
    if (!start || !end) continue;
    out.push({ key: `p-${p.id}`, who: requestWho(p), start, end, label: requestWhat(p), kind: requestKind(p), pending: true });
  }
  return out;
}

/**
 * Whether someone is away on a day.
 *
 * @param list - Absences.
 * @param who - The person.
 * @param day - The day.
 * @param approvedOnly - Ignore waiting requests.
 * @returns True when an absence covers that day.
 */
export function isAway(list: Absence[], who: string, day: Date, approvedOnly = false): boolean {
  return list.some((a) => a.who === who && (!approvedOnly || !a.pending) && a.start <= day && day <= a.end);
}

/**
 * How many of the team have approved time off during this working week.
 *
 * @param names - The team's names.
 * @param list - Absences.
 * @returns The count.
 */
export function offThisWeek(names: string[], list: Absence[]): number {
  const mon = mondayOf(today()), fri = addDays(mon, 4);
  return names.filter((n) => list.some((a) => a.who === n && !a.pending && overlaps(a.start, a.end, mon, fri))).length;
}

/**
 * Working days (Monday to Friday) from a start, for the charts. Company
 * holidays are not known to the team endpoints, so they are not skipped.
 *
 * @param from - The first day.
 * @param count - How many calendar days to cover.
 * @returns The weekdays in that span.
 */
export function workingDays(from: Date, count: number): Date[] {
  return Array.from({ length: count }, (_, i) => addDays(from, i)).filter((d) => !isWeekend(d));
}

/**
 * People in on each working day, if every waiting request were approved.
 *
 * @param names - The team's names.
 * @param list - Absences, waiting ones included.
 * @param days - The working days.
 * @returns One {day, in} per day.
 */
export function inPerDay(names: string[], list: Absence[], days: Date[]): { day: Date; in: number }[] {
  return days.map((day) => ({ day, in: names.filter((n) => !isAway(list, n, day)).length }));
}

/**
 * Days from a request arriving to its approval, for every approved request
 * that carries both dates. Only approvals stamp ApprovedDate, so rejections
 * are not counted.
 *
 * @param requests - /team/requests items.
 * @returns {month: "YYYY-MM" of the approval, days} for each.
 */
export function approveTimes(requests: any[]): { month: string; days: number; at: Date }[] {
  const out: { month: string; days: number; at: Date }[] = [];
  for (const r of requests) {
    if (r.Status !== 'Approved') continue;
    const made = parseDay(r.Created), done = parseDay(r.ApprovedDate);
    if (!made || !done) continue;
    out.push({ month: dayKey(done).slice(0, 7), days: Math.max(0, daysBetween(made, done)), at: done });
  }
  return out;
}

/**
 * The average days to approve over the last 90 days, and per month for the
 * last six months (for the sparkline).
 *
 * @param requests - /team/requests items.
 * @returns {avg (null when none), months: [{month, avg}]}.
 */
export function approveSummary(requests: any[]): { avg: number | null; months: { month: string; avg: number }[] } {
  const times = approveTimes(requests);
  const cutoff = addDays(today(), -90);
  const recent = times.filter((t) => t.at >= cutoff);
  const avg = recent.length ? recent.reduce((s, t) => s + t.days, 0) / recent.length : null;
  // The last six calendar months, oldest first; a month with no approvals is skipped.
  const now = today();
  const months: { month: string; avg: number }[] = [];
  for (let i = 5; i >= 0; i--) {
    const m = dayKey(new Date(now.getFullYear(), now.getMonth() - i, 1, 12)).slice(0, 7);
    const these = times.filter((t) => t.month === m);
    if (these.length) months.push({ month: m, avg: these.reduce((s, t) => s + t.days, 0) / these.length });
  }
  return { avg, months };
}

/** The three stacks of the monthly chart. */
export type MonthStack = 'vacation' | 'sick' | 'other';

/**
 * Approved days off this year, by the month they start in and their kind.
 *
 * @param requests - /team/requests items.
 * @param year - The year to count.
 * @returns Twelve months of {vacation, sick, other} days.
 */
export function daysOffByMonth(requests: any[], year: number): Record<MonthStack, number>[] {
  const months = Array.from({ length: 12 }, () => ({ vacation: 0, sick: 0, other: 0 }));
  for (const r of requests) {
    if (r.request_type !== 'leave' || r.Status !== 'Approved') continue;
    const start = parseDay(r.StartDate);
    if (!start || start.getFullYear() !== year) continue;
    const days = Number(r.Days || 0);
    const kind = requestKind(r);
    // Part days are vacation time; bereavement, jury duty and the rest are "other".
    const stack: MonthStack = kind === 'vacation' || kind === 'partial' ? 'vacation' : kind === 'sick' ? 'sick' : 'other';
    months[start.getMonth()][stack] += days;
  }
  return months;
}

/**
 * Approved time this year by kind, for the "By kind" bars.
 *
 * @param requests - /team/requests items.
 * @param year - The year.
 * @returns [label, days, kind] rows; overtime hours are shown as days (8 hours = 1 day).
 */
export function totalsByKind(requests: any[], year: number): [string, number, RequestKind][] {
  const t = { vacation: 0, sick: 0, overtime: 0, other: 0 };
  for (const r of requests) {
    if (r.Status !== 'Approved') continue;
    const d = parseDay(r.StartDate || r.Created);
    if (!d || d.getFullYear() !== year) continue;
    if (r.request_type === 'overtime') { t.overtime += Number(r.Hours || 0) / 8; continue; }
    if (r.request_type !== 'leave') continue;
    const k = requestKind(r);
    if (k === 'vacation' || k === 'partial') t.vacation += Number(r.Days || 0);
    else if (k === 'sick') t.sick += Number(r.Days || 0);
    else t.other += Number(r.Days || 0);
  }
  return [
    ['Vacation', t.vacation, 'vacation'],
    ['Sick', t.sick, 'sick'],
    ['Overtime (as days)', t.overtime, 'overtime'],
    ['Bereavement and jury', t.other, 'bereavement'],
  ];
}

/**
 * Who else is away during a request's dates, for the flag on its card.
 *
 * @param item - A /team/pending item.
 * @param list - Absences.
 * @returns "Riley (Oct 20 to Oct 22)" style notes, one per other absence.
 */
export function clashesFor(item: any, list: Absence[]): string[] {
  if (item.request_type !== 'leave') return [];
  const s = parseDay(item.StartDate), e = parseDay(item.EndDate) || s;
  if (!s || !e) return [];
  const me = requestWho(item);
  return list
    .filter((a) => a.who !== me && overlaps(a.start, a.end, s, e))
    .map((a) => `${a.who.split(' ')[0]} is ${a.pending ? 'asking for' : 'off'} ${shortDate(dayKey(a.start))}${a.end > a.start ? ` to ${shortDate(dayKey(a.end))}` : ''}`);
}

/**
 * A few plain notes worth a manager's attention, all worked out from the data.
 *
 * @param members - /team/members.
 * @param list - Absences, waiting ones included.
 * @returns Up to four {tone, text} notes.
 */
export function insights(members: any[], list: Absence[]): { tone: 'warn' | 'info'; text: string }[] {
  const out: { tone: 'warn' | 'info'; text: string }[] = [];
  const names = members.map((m) => m.name);
  // The thinnest day in the next four weeks, if every waiting request is approved.
  if (list.some((a) => a.pending) && names.length > 1) {
    const days = inPerDay(names, list, workingDays(today(), 28));
    const low = days.reduce((m, d) => (d.in < m.in ? d : m), days[0]);
    if (low && low.in < names.length) {
      out.push({ tone: 'warn', text: `If every waiting request is approved, ${low.in} of ${names.length} are in on ${shortDate(dayKey(low.day))}.` });
    }
  }
  // Below zero on vacation: approved leave has run past what they have.
  for (const m of members) {
    const v = m.balances?.vacation_balance ?? 0;
    if (v < 0) out.push({ tone: 'warn', text: `${m.name} is ${Math.abs(v)} vacation day${Math.abs(v) === 1 ? '' : 's'} below zero.` });
  }
  // Carried-over days are cleared every April 1.
  for (const m of members) {
    const c = m.balances?.carryover ?? 0;
    if (c > 0) out.push({ tone: 'info', text: `${m.name} has ${c} carried-over day${c === 1 ? '' : 's'}. Carried-over days reset on April 1.` });
  }
  return out.slice(0, 4);
}
