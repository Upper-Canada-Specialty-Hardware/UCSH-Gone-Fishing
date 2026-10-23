import api, { SetupScope } from './client';
import { adminSessionToken } from './adminSession';
import { BelowZero } from '../utils/employment';

/**
 * The admin requests screen's own calls (#278): create a request, delete one,
 * preview its balance effect, export to Excel, and load the new-request
 * options. They use the shared axios instance, so each carries the admin
 * session header and the staff sign-in the interceptor adds.
 */

/** The form an admin new request carries; the fields match the request page's. */
export interface AdminRequestForm {
  // leave
  leave_type?: string;
  start_date?: string;
  end_date?: string;
  partial_hours?: number;
  notes?: string;
  // overtime
  description?: string;
  date?: string;
  hours?: number;
  // carry-over / payout
  type_of_request?: string;
  days?: number;
}

/** The body POSTed to create a request as an admin. */
export interface NewRequestBody {
  request_type: string;
  employee_id: string;
  status: 'Pending' | 'Approved';
  form: AdminRequestForm;
}

/** One balance set as the preview endpoint returns it (dashboard keys). */
export interface PreviewBalances {
  vacation_balance: number;
  sick_balance: number;
  overtime: number;
  carryover: number;
  payout: number;
  [key: string]: number;
}

/** What /admin/requests/preview answers with. */
export interface PreviewResult {
  days: number;
  current: PreviewBalances;
  projected: PreviewBalances | null;
  unchanged: string;
  notes: string[];
  warning: string;
  next_year: boolean;
  /** Set when approving would take the person's floor below zero (#300); never a block. */
  below_zero: BelowZero | null;
}

/** One person the new-request dialog can pick, from the Staff Directory. */
export interface StaffOption {
  id: string;
  name: string;
  department: string;
  location: string;
  email: string;
  managers: string;
}

/**
 * Create a request on behalf of an employee.
 *
 * @param body - Type, employee, status and the form.
 * @returns The axios response ({status, request_type, item_id}).
 */
export const createAdminRequest = (body: NewRequestBody) =>
  api.post('/admin/requests/new', body);

/**
 * What a new request would do to the chosen employee's balances. Saves nothing.
 *
 * @param body - request_type, employee_id and the form.
 * @returns The preview.
 */
export const previewAdminRequest = (body: { request_type: string; employee_id: string; form: AdminRequestForm }) =>
  api.post<PreviewResult>('/admin/requests/preview', body);

/**
 * Delete one request, with a reason and an optional refund.
 *
 * @param requestType - 'leave', 'overtime' or 'carryover-payout'.
 * @param requestId - The item id.
 * @param body - reason (required) and refund (reverse an approved item first).
 * @returns The axios response ({status, refunded, ...}).
 */
export const deleteAdminRequest = (requestType: string, requestId: string, body: { reason: string; refund: boolean }) =>
  api.post(`/admin/requests/${requestType}/${requestId}/delete`, body);

/** One pot movement a settle plans or did; change is signed (negative = taken out). */
export interface SettleMove {
  pot: string;
  before: number;
  after: number;
  change: number;
}

/** What GET /settle answers: the request, the pots now, and what "take it off now" would do (#400). */
export interface SettlePreview {
  request: {
    kind: string;
    item_id: number;
    employee_name: string;
    leave_type: string | null;
    request_option: string | null;
    start_date: string | null;
    end_date: string | null;
    days: number | null;
    hours: number | null;
    decided_at: string | null;
    decided_by: string | null;
  };
  pots: Record<string, number>;
  plan: { outcome: 'balance_applied' | 'counted' | 'no_balance_used' | 'refused_negative'; moves: SettleMove[]; note: string };
  /** True for a leave that spends pots: the admin can say which pots it came from by hand. */
  hand_editable: boolean;
}

/** The three ways to settle an approved request that never moved a balance. */
export type SettleOutcome = 'now' | 'hand' | 'none';

/**
 * What settling one approved, never-applied request would do. Saves nothing.
 *
 * @param requestType - 'leave', 'overtime' or 'carryover-payout'.
 * @param requestId - The item id.
 * @param scope - 'admin', or 'team' for a manager settling a teammate's request (#463).
 * @returns The preview.
 */
export const getSettlePreview = (requestType: string, requestId: string | number, scope: SetupScope = 'admin') =>
  api.get<SettlePreview>(`/${scope}/requests/${requestType}/${requestId}/settle`);

/**
 * Settle one approved request that never moved a balance, with a reason.
 *
 * @param requestType - 'leave', 'overtime' or 'carryover-payout'.
 * @param requestId - The item id.
 * @param body - outcome, the required reason, and for a by-hand leave the days taken per pot.
 * @param scope - 'admin', or 'team' for a manager settling a teammate's request (#463).
 * @returns The axios response ({status, outcome, balance_state, moves}).
 */
export const settleRequest = (
  requestType: string,
  requestId: string | number,
  body: { outcome: SettleOutcome; reason: string; pieces?: Record<string, number> },
  scope: SetupScope = 'admin',
) => api.post<{ status: string; outcome: SettleOutcome; balance_state: string; moves: SettleMove[] }>(
  `/${scope}/requests/${requestType}/${requestId}/settle`, body,
);

/**
 * The Staff Directory people and leave types the new-request dialog needs.
 *
 * @returns The axios response ({staff, leave_types}).
 */
export const getNewRequestOptions = () =>
  api.get<{ staff: StaffOption[]; leave_types: string[] }>('/admin/requests/new-options');

/**
 * Download the shown or selected requests as an .xlsx (one sheet per list).
 *
 * @param requests - The request items the admin is viewing.
 * @returns The axios response, with a Blob body.
 */
export const exportAdminRequests = (requests: any[]) =>
  api.post<Blob>('/admin/requests/export', { requests }, { responseType: 'blob' });

/**
 * Send a deferred delete immediately when the page is hidden or unloaded.
 *
 * axios cannot run during unload, so this uses fetch with keepalive and the
 * same X-Admin-Session header the axios instance sends on /admin calls, so the
 * delete still lands if the admin closes the tab during the Undo window.
 *
 * @param requestType - The request type.
 * @param requestId - The item id.
 * @param body - reason and refund.
 */
export function flushDeleteBeacon(requestType: string, requestId: string, body: { reason: string; refund: boolean }): void {
  const base = (import.meta.env.VITE_API_URL as string) || '';
  const url = `${base}/api/dashboard/admin/requests/${requestType}/${requestId}/delete`;
  try {
    void fetch(url, {
      method: 'POST',
      keepalive: true,                                   // lets the request outlive the page
      headers: { 'Content-Type': 'application/json', 'X-Admin-Session': adminSessionToken() },
      body: JSON.stringify(body),
    });
  } catch {
    /* nothing more can be done once the page is going away */
  }
}
