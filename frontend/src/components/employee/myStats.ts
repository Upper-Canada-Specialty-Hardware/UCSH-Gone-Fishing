import { RequestKind } from '../../constants/leaveColors';
import { daysBetween, parseDay, today } from '../../utils/dates';
import { requestKind } from '../../utils/requestText';

/** One employee's year in numbers, all from their own approved requests. */
export interface MyYear {
  /** Approved leave days starting this year, every kind. */
  daysOff: number;
  /** Approved vacation and part days. */
  vacation: number;
  /** Approved sick or personal days. */
  sick: number;
  /** Approved bereavement, jury duty and other leave. */
  other: number;
  /** Approved overtime, in hours. */
  overtimeHours: number;
  /** Days moved to carry-over by approved requests made this year. */
  carriedOver: number;
  /** Days set aside for payout by approved requests made this year. */
  paidOut: number;
}

/**
 * Sum one employee's approved requests for a year. Leave counts by its start
 * date, overtime by its date, carry-over and payout by when they were asked.
 *
 * @param requests - /me/requests items.
 * @param year - The year to count.
 * @returns The totals.
 */
export function myYear(requests: any[], year: number): MyYear {
  const t: MyYear = { daysOff: 0, vacation: 0, sick: 0, other: 0, overtimeHours: 0, carriedOver: 0, paidOut: 0 };
  for (const r of requests) {
    if (r.Status !== 'Approved') continue;                                  // only what really happened
    const when = parseDay(r.request_type === 'carryover-payout' ? r.Created : (r.StartDate || r.Created));
    if (!when || when.getFullYear() !== year) continue;                     // this year only
    if (r.request_type === 'overtime') { t.overtimeHours += Number(r.Hours || 0); continue; }
    if (r.request_type === 'carryover-payout') {
      // TypeofRequest says which pot the days moved to.
      if (r.TypeofRequest === 'Payout') t.paidOut += Number(r.Days || 0);
      else t.carriedOver += Number(r.Days || 0);
      continue;
    }
    if (r.request_type !== 'leave') continue;
    const days = Number(r.Days || 0);
    const k = requestKind(r);
    t.daysOff += days;                                                     // every leave kind counts as time off
    if (k === 'vacation' || k === 'partial') t.vacation += days;           // part days come out of vacation time
    else if (k === 'sick') t.sick += days;
    else t.other += days;                                                  // bereavement, jury duty
  }
  return t;
}

/**
 * The "this year" bars, one row per kind, for the By kind panel.
 *
 * @param y - The year's totals.
 * @returns [label, days, kind] rows; overtime hours shown as days (8 hours = 1 day).
 */
export function myKindRows(y: MyYear): [string, number, RequestKind][] {
  return [
    ['Vacation', y.vacation, 'vacation'],
    ['Sick or personal', y.sick, 'sick'],
    ['Bereavement and jury', y.other, 'bereavement'],
    ['Overtime earned (as days)', y.overtimeHours / 8, 'overtime'],
    ['Carried over', y.carriedOver, 'carry'],
    ['Set aside for payout', y.paidOut, 'payout'],
  ];
}

/**
 * Days until the next April 1, when carried-over days are cleared.
 *
 * @param now - Today (for tests); defaults to today.
 * @returns Whole days, 0 on April 1 itself.
 */
export function daysToCarryReset(now: Date = today()): number {
  // April 1 this year if it has not passed yet, otherwise next year's.
  const thisYear = new Date(now.getFullYear(), 3, 1, 12);
  const next = now <= thisYear ? thisYear : new Date(now.getFullYear() + 1, 3, 1, 12);
  return Math.max(0, daysBetween(now, next));
}
