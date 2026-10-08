import type { RequestType } from '../../api/intake';
import type { PrimaryOverride } from '../../theme';
import type { RequestValues } from '../RequestForm';

/** The feel of each kind of request: warm for time off, quiet for hard days, bright for work done. */
export type Mood = 'vacation' | 'sick' | 'partial' | 'bereavement' | 'jury' | 'overtime' | 'carry' | 'payout';

/** One mood: the page's colours in each mode and the two lines on the side panel. */
interface MoodSpec {
  light: PrimaryOverride;
  dark: PrimaryOverride;
  /** Headline and the line under it; empty strings show nothing. */
  words: [string, string];
}

/** Every mood. Dark shades are lighter so the panel keeps its contrast on a dark page. */
export const MOODS: Record<Mood, MoodSpec> = {
  vacation: {
    light: { primary: '#1f7a8c', primarySoft: '#e2f2f4', primaryInk: '#ffffff', accent: '#f4a259' },
    dark: { primary: '#62b7c6', primarySoft: '#12313a', primaryInk: '#06191d', accent: '#f4a259' },
    words: ['Enjoy the time off.', 'Your manager gets the request straight away.'],
  },
  sick: {
    light: { primary: '#4b5d6e', primarySoft: '#e8ecf0', primaryInk: '#ffffff', accent: '#8fa6ba' },
    dark: { primary: '#8ea2b6', primarySoft: '#1d252e', primaryInk: '#0d1218', accent: '#8fa6ba' },
    words: ['Rest up.', 'Your manager approves it and you get an email. Feel better soon.'],
  },
  partial: {
    light: { primary: '#4f8a6b', primarySoft: '#e6f1ea', primaryInk: '#ffffff', accent: '#e9c46a' },
    dark: { primary: '#86c09f', primarySoft: '#16301f', primaryInk: '#08180e', accent: '#e9c46a' },
    words: ['A few hours away.', 'Part of a day, in half-hour steps.'],
  },
  bereavement: {
    light: { primary: '#3a3340', primarySoft: '#ebe8ee', primaryInk: '#f1edf3', accent: '#7d6e86' },
    dark: { primary: '#a596ae', primarySoft: '#241f28', primaryInk: '#141016', accent: '#7d6e86' },
    words: ['', ''],                                   // no words: the colour is enough
  },
  jury: {
    light: { primary: '#2f3b4c', primarySoft: '#e6e9ee', primaryInk: '#eef1f5', accent: '#8a96a8' },
    dark: { primary: '#95a3b8', primarySoft: '#1c222c', primaryInk: '#0d1117', accent: '#8a96a8' },
    words: ['Jury duty, covered.', 'This does not use any of your balance.'],
  },
  overtime: {
    light: { primary: '#c4610f', primarySoft: '#fcefe2', primaryInk: '#ffffff', accent: '#f6c445' },
    dark: { primary: '#f29a4a', primarySoft: '#3a220d', primaryInk: '#1e0f02', accent: '#f6c445' },
    words: ['Thank you for your hard work.', 'This request makes sure it does not go unnoticed.'],
  },
  carry: {
    light: { primary: '#4c4f9e', primarySoft: '#ebebf8', primaryInk: '#ffffff', accent: '#e07a5f' },
    dark: { primary: '#9a9cf0', primarySoft: '#23244a', primaryInk: '#0f1030', accent: '#e07a5f' },
    words: ['Save some for next year.', 'Move unused vacation days into next year.'],
  },
  payout: {
    light: { primary: '#2e7d4f', primarySoft: '#e3f3e8', primaryInk: '#ffffff', accent: '#d9a826' },
    dark: { primary: '#6cc792', primarySoft: '#133222', primaryInk: '#06180d', accent: '#d9a826' },
    words: ['Cash in unused days.', 'Unused vacation days, paid out.'],
  },
};

/** The side panel's lines before a kind is chosen. */
export const DEFAULT_WORDS: [string, string] = [
  'Time off, without the paperwork.',
  'Leave, overtime, or a carry-over or payout. No Microsoft sign-in needed.',
];

/** Leave types (as the backend words them) to their mood. */
const LEAVE_MOODS: Record<string, Mood> = {
  Vacation: 'vacation',
  'Sick or Personal Day': 'sick',
  'Half Day or Partial Day Off': 'partial',
  Bereavement: 'bereavement',
  'Jury Duty': 'jury',
};

/**
 * The mood for what the person is asking for. The email and code steps stay
 * neutral; after that the colour follows the form and, for leave, the type.
 *
 * @param signingIn - True on the email and code steps.
 * @param type - Which form.
 * @param v - The values typed so far.
 * @returns The mood, or null for the base colours.
 */
export function moodOf(signingIn: boolean, type: RequestType, v: RequestValues): Mood | null {
  if (signingIn) return null;
  if (type === 'overtime') return 'overtime';
  if (type === 'carryover-payout') return v.type_of_request === 'Payout' ? 'payout' : 'carry';
  return LEAVE_MOODS[v.leave_type] ?? null;            // no leave type chosen yet: neutral
}
