import { RefObject, useEffect, useLayoutEffect, useState } from 'react';

/**
 * No admin table scrolls sideways (the owner's rule). When a table is wider
 * than its column, the least important columns step aside by a fixed priority
 * (selection and actions kept longest), and below a phone width every row
 * becomes a labelled card instead. This hook decides which columns to drop and
 * when to switch to cards, from the real width of the container and a width
 * budget per column; the screen keeps the dropped columns' values visible in
 * each opened row and names them in the footer.
 */

/** Below this container width, rows become labelled cards. */
export const CARD_WIDTH = 560;

/** A column's assumed width in pixels; a key missing here uses DEFAULT_WIDTH. */
export type ColumnWidths = Record<string, number>;

const DEFAULT_WIDTH = 120;

/** The checkbox and chevron gutters, in pixels, that every row always spends. */
const GUTTERS = 82;

/** The actions column is never dropped, so a row's buttons are always reachable. */
const PROTECTED = 'quick';

export interface FitResult {
  /** Column keys to show, in the caller's order. */
  visible: string[];
  /** Column keys dropped to make it fit, least important first. */
  hidden: string[];
  /** True when the table should render as cards (too narrow for any table). */
  cards: boolean;
}

/**
 * Work out which columns fit, purely from numbers (no DOM), so it is easy to
 * reason about and test.
 *
 * Columns are kept most-important-first until the width budget runs out; the
 * rest drop. The selection and chevron gutters are always counted, so the real
 * row never overflows. At least the top few columns are always kept, and if
 * even those do not fit, the caller falls back to cards.
 *
 * @param width - The container's inner width in pixels.
 * @param columns - The column keys, already in importance order (most first).
 * @param widths - Each column's assumed width (DEFAULT_WIDTH when missing).
 * @param gutters - Fixed pixels for the checkbox and chevron columns.
 * @returns Which columns are visible or hidden, and whether to use cards.
 */
export function fitColumns(width: number, columns: string[], widths: ColumnWidths, gutters = GUTTERS): FitResult {
  if (width <= 0) return { visible: columns, hidden: [], cards: false };   // before first measure: show all
  if (width < CARD_WIDTH) return { visible: [], hidden: columns, cards: true };

  const budget = width - gutters;
  const visible: string[] = [];
  const hidden: string[] = [];
  let used = 0;
  for (const key of columns) {                           // most important first
    const w = widths[key] ?? DEFAULT_WIDTH;
    if (used + w <= budget || visible.length < 2) {      // always keep the first couple
      visible.push(key);
      used += w;
    } else {
      hidden.push(key);
    }
  }
  // Nothing but the first two fit: the table is pointless, use cards instead.
  if (visible.length <= 2 && hidden.length > 0) return { visible: [], hidden: columns, cards: true };
  return { visible, hidden, cards: false };
}

/**
 * Track a container's width and return the columns that fit inside it.
 *
 * @param containerRef - The element the table lives in (its inner width is the budget).
 * @param columns - Column keys in importance order (most important first).
 * @param widths - Assumed width per column key.
 * @returns The current fit: visible, hidden and whether to use cards.
 */
export function useFitColumns(
  containerRef: RefObject<HTMLElement | null>,
  columns: string[],
  widths: ColumnWidths,
): FitResult {
  const [width, setWidth] = useState(0);
  // Columns dropped beyond the width budget because the real table, once laid
  // out, still overflowed (the assumed widths under-counted a long value).
  const [extraDrop, setExtraDrop] = useState(0);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    // Measure now, then whenever the container resizes. Only the width matters,
    // so a height change from hiding a column never loops back into a refit.
    const measure = () => setWidth((prev) => {
      const next = Math.round(el.clientWidth);
      return next === prev ? prev : next;
    });
    measure();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [containerRef]);

  // A width change starts the fit over, so post-layout drops do not accumulate.
  useEffect(() => { setExtraDrop(0); }, [width]);

  const base = fitColumns(width, columns, widths);

  // After the table is on screen, if it still overflows its container (clipped,
  // never scrolled), drop one more of the lowest-priority columns and let it
  // settle. Capped so it cannot loop, and the actions column is never dropped.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el || base.cards) return;
    const overflowing = el.scrollWidth > el.clientWidth + 1;
    const droppable = base.visible.filter((k) => k !== PROTECTED).length;
    if (overflowing && extraDrop < droppable - 2) setExtraDrop((n) => n + 1);
  });

  if (base.cards || extraDrop === 0) return base;

  // Move the lowest-priority visible columns (from the end of the importance
  // order) into hidden, skipping the actions column.
  const visible = [...base.visible];
  const hidden = [...base.hidden];
  let dropped = 0;
  for (let i = visible.length - 1; i >= 0 && dropped < extraDrop; i--) {
    if (visible[i] === PROTECTED) continue;                // keep the actions column
    hidden.push(visible[i]);
    visible.splice(i, 1);
    dropped++;
  }
  if (visible.length <= 2) return { visible: [], hidden: columns, cards: true };   // nothing useful left: cards
  return { visible, hidden, cards: false };
}
