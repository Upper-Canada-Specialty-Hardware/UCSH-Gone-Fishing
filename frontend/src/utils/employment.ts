/**
 * Shared helpers for the three employment types (#300). The Staff Directory
 * SalaryHourly column holds "Salary", "Hourly" (Ontario hourly) and "Hourly BC"
 * (British Columbia hourly). BC hourly people have one real balance, Sick, and
 * a vacation counter ("days taken this year") instead of a vacation balance;
 * they have no Make-Up, Carry-over or Payout. These helpers keep that test and
 * the shared red "below zero" wording in one place so every screen reads alike.
 *
 * The matching backend lives in app/services/employment.py (is_hourly_bc,
 * floor_warning, floor_warning_text); this mirrors its wording exactly.
 */

/** The employment type the balance engine acts on (the SalaryHourly value). */
export type EmploymentType = 'Salary' | 'Hourly' | 'Hourly BC';

/**
 * A floor-below-zero warning the backend stamps on a request or preview: which
 * pot is the person's floor, and what approving would take it from and to.
 * Never a block; the manager may still approve.
 */
export interface BelowZero {
  /** The floor pot: sick for BC hourly, vacation for everyone else. */
  pot: 'sick' | 'vacation';
  /** The pot's value now. */
  before: number;
  /** The pot's value after approval (below zero). */
  after: number;
}

/**
 * Whether a person is British Columbia hourly ("Hourly BC").
 *
 * @param source - A balances object carrying `hourly_bc`, or the raw
 *   employment-type string ("Hourly BC"). Anything else reads as not BC hourly.
 * @returns True for a BC hourly person.
 */
export function isHourlyBc(source: { hourly_bc?: unknown } | string | null | undefined): boolean {
  if (typeof source === 'string') return source === 'Hourly BC';   // the SalaryHourly value itself
  return !!source?.hourly_bc;                                       // the wire flag on balances
}

/**
 * A day count trimmed like the backend's `:g` format (no trailing zeros).
 *
 * @param n - The number of days (may be negative).
 * @returns e.g. "-1.5", "-2", "3".
 */
function trimDays(n: number): string {
  return String(Math.round(n * 1000) / 1000);                      // 3 decimals, trailing zeros dropped
}

/**
 * The shared red sentence for a floor-below-zero warning, word-for-word with
 * the backend's floor_warning_text so the UI, emails and texts all match.
 *
 * @param bz - The warning the backend sent, or null.
 * @returns The sentence, or "" when there is nothing to warn about.
 */
export function belowZeroSentence(bz: BelowZero | null | undefined): string {
  if (!bz) return '';                                              // nothing to say
  const label = bz.pot === 'sick' ? 'Sick' : 'Vacation';          // the floor pot's name
  return (
    `Approving this takes ${label} below zero, to ${trimDays(bz.after)} days. `
    + "You can still approve it; that is the manager's call."
  );
}
