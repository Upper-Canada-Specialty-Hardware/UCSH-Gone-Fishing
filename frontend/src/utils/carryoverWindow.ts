/**
 * A YYYY-MM-DD date as "November 1, 2026", read as a calendar day (no time
 * zone shift).
 *
 * @param iso - The date.
 * @returns The long form.
 */
export function longDate(iso: string): string {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('en-CA', { month: 'long', day: 'numeric', year: 'numeric' });
}

/**
 * Why carry-over and payout cannot be picked, or null when they can.
 *
 * @param open - Whether today is inside the dates.
 * @param window - The dates an admin set, or null for none.
 * @param today - Today in Toronto (YYYY-MM-DD), as the server checked it.
 * @returns A short line for the greyed-out card, or null when open.
 */
export function carryoverClosedReason(open: boolean, window: { start: string; end: string } | null, today: string): string | null {
  if (open) return null;
  if (!window) return 'Not open right now. HR opens these at the time of year they are taken.';
  if (today < window.start) return `Opens ${longDate(window.start)}, until ${longDate(window.end)}.`;   // ISO dates sort as text
  return `Closed on ${longDate(window.end)}.`;
}
