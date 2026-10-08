/** Warm, readable colours for people, with a lighter shade of each for dark mode. */
const PALETTE = {
  light: ['#2f5d50', '#e07a5f', '#3d5a80', '#b8892f', '#7a5c8e', '#4f8a6b', '#c4610f', '#1f7a8c'],
  dark: ['#8cc5b2', '#ee9a80', '#8fb0dc', '#e2b75c', '#b99ad0', '#86c09f', '#f29a4a', '#62b7c6'],
};

/** A function giving each person their colour. */
export type ColorOf = (name: string) => string;

/**
 * Colours for a team: each person gets the next palette colour in name order,
 * so nobody on a team of up to eight shares one, and the same person keeps the
 * same colour on every chart. Someone not on the list falls back to a stable
 * hash of their name.
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
