/**
 * The one place the frontend turns a request's state into what to show (#455).
 *
 * The backend owns each request's state-machine position and sends it on every
 * request item (snake_case ``state``) and every live event. The dashboards only
 * render it: a label, a chip colour, and whether the Approve / Reject buttons are
 * enabled. Keeping that mapping here means the manager, admin and employee views
 * can never disagree on what a state looks like.
 *
 * A request from the SharePoint path carries no ``state``; the views then keep
 * their old look (``stateDisplay`` returns null, ``canDecide`` stays true), so
 * nothing changes until the Postgres path is live.
 */

/** The backend-owned state-machine positions (request_vocab.STATES). */
export type RequestState =
  | 'getting_ready'
  | 'waiting_on_approval'
  | 'approved'
  | 'rejected'
  | 'refunded'
  | 'cancelled'
  | 'needs_attention';

/** A MUI Chip colour name, for a state's chip. */
export type ChipColor = 'default' | 'warning' | 'info' | 'success' | 'error';

/** How one state is shown: its label, chip colour, and whether it is decidable. */
export interface StateDisplay {
  label: string;
  color: ChipColor;
  canDecide: boolean; // Approve / Reject enabled for this state
}

// One row per state. Only waiting_on_approval is decidable; getting_ready and
// needs_attention are in-progress or stuck, and the decided states are terminal.
const DISPLAY: Record<RequestState, StateDisplay> = {
  getting_ready: { label: 'Getting ready', color: 'warning', canDecide: false },
  waiting_on_approval: { label: 'Waiting on approval', color: 'info', canDecide: true },
  approved: { label: 'Approved', color: 'success', canDecide: false },
  rejected: { label: 'Rejected', color: 'error', canDecide: false },
  refunded: { label: 'Refunded', color: 'default', canDecide: false },
  cancelled: { label: 'Cancelled', color: 'default', canDecide: false },
  needs_attention: { label: 'Needs attention', color: 'error', canDecide: false },
};

/**
 * How to show a request's state, or null when there is no state to show.
 *
 * @param state - The request's ``state`` (snake_case), or null/undefined on the
 *   SharePoint path, where the view keeps its old look.
 * @returns The display for a known state, else null.
 */
export function stateDisplay(state?: string | null): StateDisplay | null {
  if (!state) return null; // SharePoint path: no state, render as before
  return DISPLAY[state as RequestState] ?? null; // an unknown value is treated as "no state"
}

/**
 * Whether Approve / Reject should be enabled for a request.
 *
 * @param state - The request's ``state``, or null/undefined on the SharePoint path.
 * @returns False only for a known non-decidable state; true when there is no
 *   state (the SharePoint path keeps today's behaviour) or the state is
 *   waiting_on_approval.
 */
export function canDecide(state?: string | null): boolean {
  const display = stateDisplay(state);
  return display ? display.canDecide : true; // no state -> keep today's behaviour
}
