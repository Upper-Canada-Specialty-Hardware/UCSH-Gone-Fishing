/**
 * Small date helpers for the dashboards. Dates travel as "YYYY-MM-DD" strings;
 * each is read at local noon so a timezone shift can never move it a day.
 */

/**
 * A Date for a "YYYY-MM-DD" string (or the date part of a timestamp).
 *
 * @param iso - The date string.
 * @returns The date at local noon, or null when unreadable.
 */
export function parseDay(iso?: string | null): Date | null {
  if (!iso) return null;
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  if (!y || !m || !d) return null;
  return new Date(y, m - 1, d, 12);
}

/**
 * "YYYY-MM-DD" for a Date, in local time.
 *
 * @param d - The date.
 * @returns The date string.
 */
export function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/**
 * The date n days after d (n may be negative).
 *
 * @param d - The start.
 * @param n - Days to add.
 * @returns A new Date.
 */
export function addDays(d: Date, n: number): Date {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}

/**
 * Today at local noon.
 *
 * @returns Today.
 */
export function today(): Date {
  const t = new Date();
  return new Date(t.getFullYear(), t.getMonth(), t.getDate(), 12);
}

/**
 * The Monday of d's week.
 *
 * @param d - Any day.
 * @returns That week's Monday.
 */
export function mondayOf(d: Date): Date {
  return addDays(d, -((d.getDay() + 6) % 7));            // Sunday counts as the end of the week
}

/**
 * Whether d is a Saturday or Sunday.
 *
 * @param d - The day.
 * @returns True on a weekend.
 */
export function isWeekend(d: Date): boolean {
  return d.getDay() === 0 || d.getDay() === 6;
}

/**
 * Whole days from a to b (b later gives a positive number).
 *
 * @param a - The earlier day.
 * @param b - The later day.
 * @returns Days between them.
 */
export function daysBetween(a: Date, b: Date): number {
  return Math.round((b.getTime() - a.getTime()) / 86400000);
}

/**
 * Whether two inclusive date ranges share a day.
 *
 * @param a1 - First range start. @param a2 - First range end.
 * @param b1 - Second range start. @param b2 - Second range end.
 * @returns True when they overlap.
 */
export function overlaps(a1: Date, a2: Date, b1: Date, b2: Date): boolean {
  return a1 <= b2 && b1 <= a2;
}
