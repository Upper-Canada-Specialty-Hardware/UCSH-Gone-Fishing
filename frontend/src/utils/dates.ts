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

/** Three-letter month names, index 0 = Jan, for the one date format the dashboards show. */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * A Date as "Mon D, YYYY" (e.g. "Nov 2, 2026"), the house date style.
 *
 * @param d - The date to label.
 * @returns The formatted day.
 */
function label(d: Date): string {
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;   // month name, day, four-digit year
}

/**
 * A calendar date ("YYYY-MM-DD", or the date part of a timestamp) as "Nov 2, 2026".
 * The date part is read as written, so no timezone ever shifts the day.
 *
 * @param iso - The date string.
 * @returns The formatted day, or '' when missing or unreadable.
 */
export function formatDay(iso?: string | null): string {
  const d = parseDay(iso);                 // reads the date part at local noon, no shift
  return d ? label(d) : '';                // '' when absent or malformed
}

/**
 * A timestamp as the reader's LOCAL day, "Nov 2, 2026". An instant late in the
 * day (e.g. 9pm Toronto) keeps its local date instead of rolling to the next UTC
 * day. A bare "YYYY-MM-DD" (no time) is a calendar date, read as written.
 *
 * @param iso - The ISO 8601 timestamp, or a plain date.
 * @returns The formatted local day, or '' when missing or unreadable.
 */
export function formatInstantDay(iso?: string | null): string {
  if (!iso) return '';                     // nothing to show
  if (!iso.includes('T')) return formatDay(iso);   // a bare date is calendar, no shift
  const d = new Date(iso);                 // a real instant, read in the browser's local zone
  return Number.isNaN(d.getTime()) ? '' : label(d);   // '' on an unparseable value
}

/**
 * A start/end pair as one phrase: a single day when they match, "Jun 12 to Jun
 * 13, 2026" within one year, "Dec 29, 2026 to Jan 2, 2027" across years. Both
 * ends are calendar dates, read as written.
 *
 * @param start - The first day.
 * @param end - The last day (may be missing, or equal to start).
 * @returns The range phrase, or '' when start is missing or unreadable.
 */
export function formatDayRange(start?: string | null, end?: string | null): string {
  const a = parseDay(start);               // the first day
  if (!a) return '';                       // no start, nothing to show
  const b = parseDay(end) || a;            // a missing end means a single day
  if (dayKey(a) === dayKey(b)) return label(a);   // one day: just that day
  const sameYear = a.getFullYear() === b.getFullYear();   // drop the first year when they match
  const first = sameYear ? `${MONTHS[a.getMonth()]} ${a.getDate()}` : label(a);   // "Jun 12" or "Dec 29, 2026"
  return `${first} to ${label(b)}`;        // the end always carries its year
}
