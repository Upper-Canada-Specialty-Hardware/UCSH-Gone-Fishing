import { today } from './dates';

/**
 * The names of the two vacation lines staff see (#551).
 *
 * The current-year line is the person's vacation for this year. The second line
 * (stored as CarryOver) is named by its dates, never "carry-over": from April to
 * December it holds days for January 1 to March 31 of next year; from January to
 * March it holds what is left from last year, until March 31.
 */
export interface VacationLabels {
  /** The current-year line's name, e.g. "2026 vacation". */
  current: string;
  /** The second line's name: "Next year" or "From 2026". */
  next: string;
  /** One short line under the second one, e.g. "for Jan 1 to Mar 31, 2027". */
  nextNote: string;
}

/**
 * The labels for a given day.
 *
 * @param on - The day to label for; defaults to today.
 * @returns The two names and the note.
 */
export function vacationLabels(on: Date = today()): VacationLabels {
  const year = on.getFullYear();                     // the calendar year staff are in
  const earlyYear = on.getMonth() <= 2;              // Jan to Mar: the second line holds last year's days
  return {
    current: `${year} vacation`,
    next: earlyYear ? `From ${year - 1}` : 'Next year',
    nextNote: earlyYear ? 'until Mar 31, used first' : `for Jan 1 to Mar 31, ${year + 1}`,
  };
}
