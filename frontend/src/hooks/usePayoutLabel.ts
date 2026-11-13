import { useEffect, useState } from 'react';
import { getRequestTypes } from '../api/intake';
import { today } from '../utils/dates';

/** The payout year once read, shared by every screen so the window is asked for once a page load. */
let cachedYear: number | null = null;

/**
 * The year a payout belongs to: the year of the payout window HR set in the
 * admin dashboard (#554), so a window that opens in January still names the
 * year the days were earned in when HR sets it that way. With no window set,
 * the current year.
 *
 * @param window - The payout window (ISO dates), or null when none is set.
 * @param on - The day to fall back to; defaults to today.
 * @returns The year, e.g. 2026.
 */
export function payoutYearFrom(window: { start: string; end: string } | null, on: Date = today()): number {
  return window ? Number(window.start.slice(0, 4)) : on.getFullYear();   // the window's start year wins
}

/**
 * The payout total's label, e.g. "2026 payout", from the admin payout window.
 * Shows the current year until the window has been read, and keeps it if the
 * read fails.
 *
 * @returns The label.
 */
export function usePayoutLabel(): string {
  const [year, setYear] = useState<number>(cachedYear ?? today().getFullYear());
  useEffect(() => {
    if (cachedYear !== null) return;                           // already read on this page load
    let live = true;                                           // ignore a reply after unmount
    getRequestTypes()
      .then(({ data }) => {
        cachedYear = payoutYearFrom(data.carryover_payout_window);
        if (live) setYear(cachedYear);
      })
      .catch(() => {});                                        // keep the current year on a failed read
    return () => { live = false; };
  }, []);
  return `${year} payout`;
}
