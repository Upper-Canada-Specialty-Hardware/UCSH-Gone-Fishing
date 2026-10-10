/** Warm, readable colours for people, with a lighter shade of each for dark mode. */
const PALETTE = {
  // Light mode: mid-dark hues so white (or the dark background) reads on top.
  light: [
    '#2f5d50', '#e07a5f', '#3d5a80', '#b8892f', '#7a5c8e', '#4f8a6b',
    '#c4610f', '#1f7a8c', '#a23b56', '#5e6b2f', '#8c4a7d', '#2b6ca3',
    '#9c5a2b', '#4a7d57', '#6b5ba3', '#b5733f', '#3a7d7a', '#8a4b3c',
  ],
  // Dark mode: the same hues lightened so they read on the dark surface.
  dark: [
    '#8cc5b2', '#ee9a80', '#8fb0dc', '#e2b75c', '#b99ad0', '#86c09f',
    '#f29a4a', '#62b7c6', '#e894a8', '#c0cb82', '#d59ec6', '#7fb6e0',
    '#e2a97a', '#8bc69a', '#a89ad8', '#e0ab7f', '#7ec4bf', '#d59a89',
  ],
};

/** A function giving each person their colour. */
export type ColorOf = (name: string) => string;

/** A function giving each person a short, unambiguous label for a team. */
export type LabelOf = (name: string) => string;

/**
 * Colours for a team: each person gets the next palette colour in name order,
 * so nobody on a team of up to eighteen shares one, and the same person keeps
 * the same colour on every chart. Someone not on the list falls back to a
 * stable hash of their name.
 *
 * @param names - The team's names, in display order.
 * @param mode - 'light' or 'dark', for the matching shade.
 * @returns A name-to-colour function.
 */
export function teamColors(names: string[], mode: 'light' | 'dark'): ColorOf {
  const pal = PALETTE[mode];
  const index = new Map(names.map((n, i) => [n, i]));
  return (name: string) => {
    const i = index.get(name);
    if (i !== undefined) return pal[i % pal.length];
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;   // simple string hash
    return pal[h % pal.length];
  };
}

/**
 * The first name, for tight spaces like timeline labels.
 *
 * @param name - The full name.
 * @returns The first word.
 */
export function firstName(name: string): string {
  return (name || '').split(' ')[0];
}

/**
 * The first name plus the last name's initial, e.g. "David K". Falls back to
 * the first name alone when there is no second word.
 *
 * @param name - The full name.
 * @returns "First L", or just the first name.
 */
function firstPlusLastInitial(name: string): string {
  const parts = (name || '').trim().split(/\s+/);   // words, ignoring extra spaces
  const first = parts[0] || '';                     // the given name
  const last = parts.length > 1 ? parts[parts.length - 1] : '';   // the surname, if any
  return last ? `${first} ${last[0]}` : first;      // add the surname's first letter when present
}

/**
 * Short labels for a team that stay unambiguous. Each person is shown by first
 * name; when another member shares that first name they become "First L" (first
 * name plus last initial); when that still collides they fall back to the full
 * name. Someone not on the team keeps their first name.
 *
 * @param names - The team's names.
 * @returns A name-to-label function.
 */
export function teamLabeler(names: string[]): LabelOf {
  // Count how many members share each lowercased first name.
  const firstCounts = new Map<string, number>();
  // Count how many share each lowercased "first + last initial".
  const flCounts = new Map<string, number>();
  for (const n of names) {
    const f = firstName(n).toLowerCase();                       // key: first name
    firstCounts.set(f, (firstCounts.get(f) || 0) + 1);          // tally the first name
    const fl = firstPlusLastInitial(n).toLowerCase();           // key: first + last initial
    flCounts.set(fl, (flCounts.get(fl) || 0) + 1);              // tally that form
  }
  return (name: string) => {
    const f = firstName(name);                                  // candidate: first name
    if ((firstCounts.get(f.toLowerCase()) || 0) <= 1) return f; // unique first name wins
    const fl = firstPlusLastInitial(name);                      // candidate: first + last initial
    if ((flCounts.get(fl.toLowerCase()) || 0) <= 1) return fl;  // unique with last initial
    return name;                                                // still clashing: use the full name
  };
}
