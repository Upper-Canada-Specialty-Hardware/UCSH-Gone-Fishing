import { today } from './dates';

/**
 * The names of the two vacation lines staff see (#551).
 *
 * The current-year line is the person's vacation for this year. The second line
 * (stored as CarryOver) is their carried-over days: usable any time, for any
 * date, until they expire on March 31. Unused vacation lands there on its own on
 * January 1.
 */
export interface VacationLabels {
  /** The current-year line's name, e.g. "2026 vacation". */
  current: string;
  /** The second line's name. */
  next: string;
  /** One short line under the second one: its deadline, e.g. "use by Mar 31, 2027". */
  nextNote: string;
  /** The yearly payout total's name, e.g. "2026 payout": the 5-day cap is per year. */
  payout: string;
}

/**
 * The labels for a given day.
 *
 * @param on - The day to label for; defaults to today.
 * @returns The two names and the deadline note.
 */
export function vacationLabels(on: Date = today()): VacationLabels {
  const year = on.getFullYear();                     // the calendar year staff are in
  // Jan to Mar: the carried days expire this Mar 31; Apr to Dec: next year's.
  const expires = on.getMonth() <= 2 ? year : year + 1;
  return {
    current: `${year} vacation`,
    // Named by the year the days come from, so nobody reads them as last year's by mistake.
    next: `Carried over from ${expires - 1}`,
    nextNote: `use by Mar 31, ${expires}`,
    payout: `${year} payout`,
  };
}
