import { RequestKind } from '../constants/leaveColors';

/**
 * The pure side of the "Taken from" feature (#296): the wire types the backend
 * sends on every request row, the labels and colours each balance pot uses, and
 * the helpers that turn a taken_from value into the chips a screen renders. All
 * of it is pure (no JSX, no I/O); the shared {@link TakenFrom} component draws it.
 *
 * The backend (services/taken_from.py) is the source of truth for an approved
 * request (read back from its audit log) and for a pending one (projected with
 * the same simulate_* functions approval uses). The one thing worked out here is
 * the request page preview, which has no server taken_from yet: see
 * {@link takenFromPreview}.
 */

/** The five balance pots a request can move, as the wire (snake_case) names them. */
export type Pot = 'sick' | 'make_up' | 'carry_over' | 'vacation' | 'payout';

/**
 * How a row's taken_from reads: recorded = approved, from its audit log;
 * projected = pending, what approving today would do; none = cost nothing, or
 * rejected/cancelled; unrecorded = approved but no usable log; refunded = the
 * reversed approval's original movements.
 */
export type TakenState = 'recorded' | 'projected' | 'none' | 'unrecorded' | 'refunded';

/** One balance movement: signed days (negative = taken out), a year only for carry-over/payout. */
export interface TakenPiece {
  pot: Pot;
  days: number;
  year: number | null;
}

/** The taken_from object every request row carries (backend services/taken_from.py). */
export interface TakenFromValue {
  state: TakenState;
  pieces: TakenPiece[];
}

/** The display order pieces read in: the spending cascade, then payout last (mirrors the backend). */
const POT_ORDER: Pot[] = ['sick', 'make_up', 'carry_over', 'vacation', 'payout'];

/** The plain pot name shown in a chip. Only carry-over and payout ever carry a year. */
const POT_NAME: Record<Pot, string> = {
  sick: 'sick',
  make_up: 'Make-Up',
  carry_over: 'carry-over',
  vacation: 'vacation',
  payout: 'payout',
};

/** The leave-colour kind each pot borrows, so a chip matches the type pills elsewhere. */
const POT_KIND: Record<Pot, RequestKind> = {
  sick: 'sick',
  make_up: 'overtime',       // Make-Up is banked overtime
  carry_over: 'carry',
  vacation: 'vacation',
  payout: 'payout',
};

/** Dashboard balance key for each pot, used to read a preview's current/projected values. */
const PREVIEW_KEY: Record<Pot, keyof BalanceSnapshot> = {
  sick: 'sick_balance',
  make_up: 'overtime',
  carry_over: 'carryover',
  vacation: 'vacation_balance',
  payout: 'payout',
};

/** The five balance values the preview endpoints return (dashboard keys). */
export interface BalanceSnapshot {
  vacation_balance: number;
  sick_balance: number;
  overtime: number;
  carryover: number;
  payout: number;
}

/**
 * A chip's label: the pot name, with a year before it only for carry-over and payout.
 *
 * @param pot - The balance pot.
 * @param year - The year its days were earned, or null.
 * @returns e.g. "vacation", "Make-Up", "2025 carry-over", "2026 payout".
 */
export function potLabel(pot: Pot, year: number | null): string {
  const name = POT_NAME[pot];                                   // base pot name
  const hasYear = year != null && (pot === 'carry_over' || pot === 'payout');   // only these two name a year
  return hasYear ? `${year} ${name}` : name;
}

/**
 * The leave-colour kind a pot's chip uses.
 *
 * @param pot - The balance pot.
 * @returns The RequestKind whose colour the chip borrows.
 */
export function potKind(pot: Pot): RequestKind {
  return POT_KIND[pot];
}

/**
 * Days as a trimmed, absolute string (sign is carried by the chip wording, not here).
 *
 * @param days - Signed days.
 * @returns The absolute amount, trimmed, e.g. "1.5", "0.125", "3".
 */
export function fmtDays(days: number): string {
  return String(Math.round(Math.abs(days) * 1000) / 1000);     // abs, trimmed to 3 decimals
}

/** One rendered unit of a taken_from value; the component draws each as one or two chips. */
export type TakenSegment =
  | { type: 'taken'; pot: Pot; days: number; year: number | null }        // days taken out of a pot
  | { type: 'add'; pot: Pot; days: number }                               // overtime added to Make-Up
  | { type: 'move'; days: number; toPot: Pot; toYear: number | null };    // vacation moved into carry-over/payout

/**
 * The pieces grouped into the segments a request type produces. The three
 * categories are mutually exclusive: a carry-over/payout request moves vacation
 * into a pot, overtime adds to Make-Up, and everything else only takes.
 *
 * @param pieces - The signed per-pot pieces.
 * @returns The segments to draw, in reading order.
 */
function segmentsOf(pieces: TakenPiece[]): TakenSegment[] {
  // A carry-over or payout request: a positive carry_over/payout piece, vacation moving in.
  const move = pieces.find((p) => (p.pot === 'carry_over' || p.pot === 'payout') && p.days > 0);
  if (move) return [{ type: 'move', days: move.days, toPot: move.pot, toYear: move.year }];

  // Overtime: a positive make_up piece added to the Make-Up pot.
  const add = pieces.find((p) => p.pot === 'make_up' && p.days > 0);
  if (add) return [{ type: 'add', pot: 'make_up', days: add.days }];

  // Otherwise every negative piece is days taken out of that pot, already in cascade order.
  return pieces.filter((p) => p.days < 0).map((p) => ({ type: 'taken', pot: p.pot, days: p.days, year: p.year }));
}

/** What the component should draw: a muted one-line message, or a set of chip segments. */
export type TakenShape =
  | { kind: 'message'; text: string }
  | { kind: 'segments'; segments: TakenSegment[]; projected: boolean; refunded: boolean };

/**
 * Turn a taken_from value into something to render. Handles the empty states
 * (nothing used, not recorded, a carry-over/payout that would be refused) as a
 * muted message, and the rest as chip segments flagged projected or refunded.
 *
 * @param value - The row's taken_from, or null/undefined when the field is absent.
 * @returns A {@link TakenShape} the component draws.
 */
export function describeTakenFrom(value: TakenFromValue | null | undefined): TakenShape {
  const state = value?.state ?? 'unrecorded';                  // a missing field reads as unrecorded
  const pieces = value?.pieces ?? [];

  if (state === 'none') return { kind: 'message', text: 'No balance used' };
  if (state === 'unrecorded') return { kind: 'message', text: 'Not recorded' };
  // A pending carry-over/payout that would push vacation below zero returns no pieces: it is auto-refused.
  if (state === 'projected' && pieces.length === 0) return { kind: 'message', text: 'Would be refused: not enough vacation' };
  if (pieces.length === 0) return { kind: 'message', text: 'No balance used' };   // any other empty set moved nothing

  const segments = segmentsOf(pieces);
  if (!segments.length) return { kind: 'message', text: 'No balance used' };      // only zero/positive noise left
  return { kind: 'segments', segments, projected: state === 'projected', refunded: state === 'refunded' };
}

/**
 * The year a carry-over or payout belongs to, by when its change happens.
 *
 * Carry-over stays usable until the April 1 reset, so a change dated January 1
 * to March 31 of year Y is still last year's carry-over and reads as Y - 1; from
 * April 1 on it is the new year's, Y. Payout uses the same boundary so the two
 * label consistently. This matches _earned_year in the backend; it lives here so
 * the request page preview (which has no server taken_from) labels the same way.
 *
 * @param day - The date the change happens (today in Toronto for a preview).
 * @returns The earned year.
 */
export function earnedYear(day: Date): number {
  return day.getMonth() <= 2 ? day.getFullYear() - 1 : day.getFullYear();   // Jan-Mar (0-2) -> last year
}

/**
 * Build a projected taken_from for the request page, from the preview's current
 * and projected balances, with the same year rule the backend uses. There is no
 * server taken_from for an unsent request, so this is the one place the split is
 * computed on the client; it reuses the preview the server already returns, so
 * the numbers cannot drift from the real deduction.
 *
 * @param current - Balances now.
 * @param projected - Balances after approval, or null when the request would be refused.
 * @param today - Today (in Toronto for the preview), for the carry-over/payout year.
 * @returns A projected taken_from value ready for {@link describeTakenFrom}.
 */
export function takenFromPreview(
  current: BalanceSnapshot,
  projected: BalanceSnapshot | null,
  today: Date,
): TakenFromValue {
  if (!projected) return { state: 'projected', pieces: [] };   // refused: no movement to show
  const year = earnedYear(today);                              // the year carry-over/payout would name
  const pieces: TakenPiece[] = [];
  for (const pot of POT_ORDER) {                               // keep the wire's reading order
    const delta = Math.round((projected[PREVIEW_KEY[pot]] - current[PREVIEW_KEY[pot]]) * 1000) / 1000;   // signed change
    if (delta === 0) continue;                                 // a pot that did not move is left out
    pieces.push({ pot, days: delta, year: pot === 'carry_over' || pot === 'payout' ? year : null });
  }
  return { state: 'projected', pieces };
}
