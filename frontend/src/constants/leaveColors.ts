/**
 * One colour per kind of request, used everywhere a kind is shown (pills,
 * calendar bars, the request page). Warm and bright for time off and work
 * done, muted and dark for hard days. Each has a light and a dark-mode shade.
 */
export type RequestKind =
  | 'vacation' | 'partial' | 'sick' | 'bereavement' | 'jury' | 'overtime' | 'carry' | 'payout';

export const KIND_COLORS: Record<RequestKind, { light: string; dark: string }> = {
  vacation: { light: '#1f7a8c', dark: '#62b7c6' },     // sea teal
  partial: { light: '#4f8a6b', dark: '#86c09f' },      // soft sage
  sick: { light: '#4b5d6e', dark: '#8ea2b6' },         // muted slate
  bereavement: { light: '#3a3340', dark: '#a596ae' },  // dark plum
  jury: { light: '#2f3b4c', dark: '#95a3b8' },         // dark navy
  overtime: { light: '#c4610f', dark: '#f29a4a' },     // productive orange
  carry: { light: '#4c4f9e', dark: '#9a9cf0' },        // indigo, saving for later
  payout: { light: '#2e7d4f', dark: '#6cc792' },       // money green
};

/** Leave type names exactly as SharePoint stores them, mapped to their kind. */
const LEAVE_KIND: Record<string, RequestKind> = {
  Vacation: 'vacation',
  'Sick or Personal Day': 'sick',
  'Half Day or Partial Day Off': 'partial',
  'Work from Home': 'partial',
  Bereavement: 'bereavement',
  'Jury Duty': 'jury',
};

/**
 * The kind of a request, from its type and SharePoint fields.
 *
 * @param requestType - 'leave', 'overtime' or 'carryover-payout'.
 * @param leaveTypeOrRequest - LeaveType for leave; TypeofRequest ('Carry Over'/'Payout') for the third.
 * @returns The kind, or null when it cannot be told.
 */
export function kindOf(requestType: string, leaveTypeOrRequest?: string): RequestKind | null {
  if (requestType === 'overtime') return 'overtime';
  if (requestType === 'carryover-payout') return leaveTypeOrRequest === 'Payout' ? 'payout' : 'carry';
  return LEAVE_KIND[leaveTypeOrRequest || ''] || null;
}

export const FALLBACK_COLOR = '#6b6257';

/**
 * The colour for a kind in the current mode.
 *
 * @param kind - The kind (null gives the fallback).
 * @param mode - 'light' or 'dark'.
 * @returns A hex colour.
 */
export function kindColor(kind: RequestKind | null, mode: 'light' | 'dark'): string {
  return kind ? KIND_COLORS[kind][mode] : FALLBACK_COLOR;
}

/** Leave type to colour (light shades), for components that colour by leave type. */
export const LEAVE_COLORS: Record<string, string> = Object.fromEntries(
  Object.entries(LEAVE_KIND).map(([type, kind]) => [type, KIND_COLORS[kind].light]),
);
