import axios from 'axios';
import { BelowZero } from '../utils/employment';
import { dropLaunchCache } from './launchCache';

const API_URL = import.meta.env.VITE_API_URL || '';

/**
 * Calls for the landing and request pages, and the one sign-in every page
 * shares (loadSession). Kept apart from the dashboard client (./client.ts),
 * which sends any 401 back to the landing page, because the request page
 * handles an expired sign-in itself by asking for a new code.
 */
const intake = axios.create({ baseURL: `${API_URL}/api/intake` });
const dashboard = axios.create({ baseURL: `${API_URL}/api/dashboard` });

/** The signed employee token /verify returns; the same shape the emailed dashboard links carry. */
export interface EmployeeSession {
  name: string;
  role: string;
  uid: string;
  token: string;
  exp: string;
  email: string;
}

/**
 * One staff record on a shared mailbox, as /verify lists them for the person to
 * pick: who it is, where they work, and that record's own signed token.
 */
export interface SignInChoice {
  name: string;
  department: string;
  location: string;
  role: string;
  uid: string;
  token: string;
  exp: string;
}

export type RequestType = 'leave' | 'overtime' | 'carryover-payout';

/** What the page needs to know about carry-over and payout right now. */
export interface RequestTypesInfo {
  carryover_payout_open: boolean;
  /** The dates an admin set (YYYY-MM-DD), or null when none are set. */
  carryover_payout_window: { start: string; end: string } | null;
  /** Today in Toronto, as the server checked it. */
  today: string;
}

/**
 * Which request types the page may offer right now.
 *
 * @returns Whether carry-over and payout are open, and the dates they open between.
 */
export const getRequestTypes = () => intake.get<RequestTypesInfo>('/request-types');

const SESSION_KEY = 'request_page_session';   // localStorage key for the 30-day sign-in
const LINK_KEYS = ['dashboard_token', 'dashboard_role', 'dashboard_uid', 'dashboard_exp'];   // sessionStorage, from an emailed link
const NOTICE_KEY = 'sign_in_notice';           // sessionStorage: why someone was sent to the landing page
const AFTER_KEY = 'after_sign_in';             // sessionStorage: the page to open once signed in
/** sessionStorage flag: this visit came from an old "add this new hire" email link (#525). */
export const RETIRED_HELD_LINK = 'retired_held_link';

/**
 * Whether a sign-in's expiry (unix seconds) has passed.
 *
 * @param exp - The token's exp field.
 * @returns True once it has expired.
 */
const expired = (exp: string) => Number(exp) * 1000 <= Date.now();

/**
 * The sign-in from an emailed dashboard link, kept for this browser tab only.
 *
 * @returns The link's token fields as a session (no name or email), or null.
 */
function linkSession(): EmployeeSession | null {
  const [token, role, uid, exp] = LINK_KEYS.map((k) => sessionStorage.getItem(k));
  if (!token || !role || !uid || !exp) return null;
  if (expired(exp)) {
    LINK_KEYS.forEach((k) => sessionStorage.removeItem(k));
    return null;
  }
  return { token, role, uid, exp, name: '', email: '' };
}

/**
 * This device's 30-day sign-in from the emailed code.
 *
 * @returns The session, or null when there is none or it expired (then removed).
 */
function deviceSession(): EmployeeSession | null {
  const raw = localStorage.getItem(SESSION_KEY);
  if (!raw) return null;
  const session = JSON.parse(raw) as EmployeeSession;
  if (expired(session.exp)) {
    localStorage.removeItem(SESSION_KEY);
    return null;
  }
  return session;
}

/**
 * The one sign-in every page uses. An emailed link opened in this tab wins,
 * so a manager's link works even on a computer someone else signed in on;
 * otherwise this device's 30-day sign-in from the emailed code.
 *
 * @returns The session, or null when nobody is signed in, it expired, or storage is blocked.
 */
export function loadSession(): EmployeeSession | null {
  try {
    return linkSession() ?? deviceSession();
  } catch {
    return null;                                       // private window or blocked storage
  }
}

/**
 * Whether a sign-in opens My team (the manager dashboard).
 *
 * @param session - The current sign-in.
 * @returns True for the manager and admin roles.
 */
export const canSeeTeam = (session: EmployeeSession | null) =>
  !!session && (session.role === 'manager' || session.role === 'admin');

/**
 * Keep an emailed dashboard link's sign-in for this browser tab.
 *
 * @param fields - token, role, uid and exp from the link.
 */
export function saveLinkSession(fields: { token: string; role: string; uid: string; exp: string }): void {
  try {
    sessionStorage.setItem('dashboard_token', fields.token);
    sessionStorage.setItem('dashboard_role', fields.role);
    sessionStorage.setItem('dashboard_uid', fields.uid);
    sessionStorage.setItem('dashboard_exp', fields.exp);
  } catch {
    /* storage blocked: the pages ask for a code instead */
  }
}

/**
 * Remember the sign-in on this device for its lifetime (30 days).
 *
 * @param session - From verifyCode.
 */
export function saveSession(session: EmployeeSession): void {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } catch {
    /* storage blocked: they sign in again next time */
  }
}

/** The role check's answer: unchanged, or a new token with the right role. */
interface RefreshAnswer {
  changed: boolean;
  role?: string;
  uid?: string;
  token?: string;
  exp?: string;
}

let roleCheck: Promise<boolean> | null = null;   // one check per page load, shared by every caller
let roleChecked = false;                          // true once that check has answered

/** @returns Whether this page load's role check has answered yet. */
export const sessionRoleChecked = () => roleChecked;

/**
 * Swap a refreshed token into wherever the sign-in lives (this tab's emailed
 * link, or the device's 30-day sign-in), keeping the name and email.
 *
 * @param fresh - The new token fields.
 * @returns True when the stored sign-in now carries the new role.
 */
function replaceSessionToken(fresh: { role: string; uid: string; token: string; exp: string }): boolean {
  try {
    if (linkSession()) saveLinkSession(fresh);                       // an emailed link wins, as in loadSession
    else {
      const current = deviceSession();
      if (!current) return false;                                    // signed out meanwhile
      saveSession({ ...current, ...fresh });                         // keep name and email
    }
  } catch {
    return false;                                                    // storage blocked
  }
  return loadSession()?.role === fresh.role;                         // only report success when it stuck
}

/**
 * Re-check the signed-in person's role once per page load (#376).
 *
 * The role is decided when the code is checked and kept for 30 days, so
 * someone listed as a manager after signing in would never see My team. This
 * asks the server again and, when it hands back a new token, stores it.
 *
 * The server only ever promotes an employee to manager; it never demotes a
 * manager back to employee (#395), so an admin's open-as-manager link keeps
 * My team (and Add Employee)
 * even with nobody under them. This call just applies whatever the server
 * returns, so it too can only move someone up.
 *
 * @returns True when the role changed and the new token was stored (the
 *   caller reloads so every part of the page sees it); false otherwise,
 *   including when nobody is signed in or the check failed.
 */
export function refreshSessionRole(): Promise<boolean> {
  if (roleCheck) return roleCheck;                                   // already asked on this page load
  const session = loadSession();
  if (!session) {
    roleChecked = true;
    return Promise.resolve(false);
  }
  const { token, role, uid, exp } = session;
  roleCheck = intake.post<RefreshAnswer>('/session/refresh', { token, role, uid, exp })
    .then((res) => {
      const a = res.data;
      if (!a.changed || !a.role || !a.uid || !a.token || !a.exp) return false;   // nothing to swap
      return replaceSessionToken({ role: a.role, uid: a.uid, token: a.token, exp: a.exp });
    })
    .catch(() => false)                                              // offline or refused: keep what they have
    .finally(() => { roleChecked = true; });
  return roleCheck;
}

/** Sign out everywhere: this device's 30-day sign-in and this tab's link. */
export function clearSession(): void {
  try {
    localStorage.removeItem(SESSION_KEY);
    LINK_KEYS.forEach((k) => sessionStorage.removeItem(k));
  } catch {
    /* nothing to clear */
  }
  void dropLaunchCache('staff');                 // the saved dashboard data goes with the sign-in
}

/**
 * The key one dashboard's data is saved under for a fast relaunch.
 *
 * A manager signs in once but opens two dashboards (their team and their own
 * requests), each saving a different shape, so the view is part of the key;
 * keyed by the sign-in alone, one view would load the other's data and break.
 *
 * @param view - Which dashboard: 'team' (manager) or 'me' (own requests).
 * @returns "<view>:<role>:<uid>" (e.g. "team:manager:368"), or null when nobody is signed in.
 */
export function staffCacheKey(view: 'team' | 'me'): string | null {
  const session = loadSession();
  return session ? `${view}:${session.role}:${session.uid}` : null;
}

/**
 * Store a value for this tab, ignoring blocked storage.
 *
 * @param key - The sessionStorage key.
 * @param value - The value, or null to remove it.
 */
function tabSet(key: string, value: string | null): void {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    /* blocked: the landing page just shows no note */
  }
}

/**
 * Read a tab value once and remove it.
 *
 * @param key - The sessionStorage key.
 * @returns The value, or '' when there is none.
 */
function tabTake(key: string): string {
  try {
    const value = sessionStorage.getItem(key) || '';
    sessionStorage.removeItem(key);
    return value;
  } catch {
    return '';
  }
}

/**
 * Leave a note for the landing page ("Sign in to open My team.").
 *
 * @param text - What the person should read above the sign-in box.
 */
export const setSignInNotice = (text: string) => tabSet(NOTICE_KEY, text);

/** @returns The landing page's note, once; '' when there is none. */
export const takeSignInNotice = () => tabTake(NOTICE_KEY);

/**
 * Remember the page to open once the person has signed in.
 *
 * @param path - A route such as '/team'.
 */
export const setAfterSignIn = (path: string) => tabSet(AFTER_KEY, path);

/** @returns The page to open after sign-in, once; '' when none was asked for. */
export const takeAfterSignIn = () => tabTake(AFTER_KEY);

/**
 * Ask for a 6-digit code to be emailed. The answer is the same for every address.
 *
 * @param email - The address typed.
 * @throws AxiosError 400 for a malformed address, 502 when the email could not be sent.
 */
export const requestCode = (email: string) => intake.post('/code', { email });

/**
 * Check a code.
 *
 * @param email - The same address.
 * @param code - The 6 digits typed.
 * @returns {status: "employee", name, role, uid, token, exp}; or {status: "choose", people}
 *   when several staff records share the address.
 * @throws AxiosError 400 with a readable reason when the code is wrong or expired; 403
 *   for an archived address or one on no staff record (ask your manager, #526).
 */
export const verifyCode = (email: string, code: string) =>
  intake.post('/verify', { email, code });

/**
 * What the public cancel page shows for a receipt's "cancel this request" link (#519).
 * ``state`` is "open" while it can still be cancelled, "done" right after this
 * page cancelled it, "invalid" for a bad link, else the request's status now.
 */
export interface CancelInfo {
  state: string;
  request_number: string;
  summary: string;
}

/**
 * The details for the public cancel page (#519). Read-only.
 *
 * @param token - The signed cancel token from the receipt's link.
 * @returns The request's summary and current state.
 */
export const getCancelInfo = (token: string) =>
  intake.get<CancelInfo>('/cancel/info', { params: { token } });

/**
 * Cancel the request from the public page's confirm button (#519).
 *
 * @param token - The signed cancel token.
 * @returns The outcome (``done`` on a fresh cancel, else what it is now).
 * @throws AxiosError 503 while processing is off.
 */
export const confirmCancel = (token: string) =>
  intake.post<CancelInfo>('/cancel/confirm', null, { params: { token } });

/** One of the person's own pending requests that holds the days they asked for (#518). */
export interface PendingClash {
  item_id: string;
  /** The dates in words, e.g. "Nov 2, 2026 to Nov 6, 2026". */
  when: string;
  leave_type: string;
  days: number | null;
}

/** The 409 body when the new leave lands on days of the person's own pending request (#518). */
export interface PendingClashDetail {
  code: 'pending_clash';
  message: string;
  pending: PendingClash[];
}

/**
 * The pending-clash offer from a failed submit, when that is what it was (#518).
 *
 * @param err - What axios threw.
 * @returns The offer, or null for any other failure.
 */
export function pendingClashOf(err: any): PendingClashDetail | null {
  const detail = err?.response?.data?.detail;                  // FastAPI puts the body under "detail"
  return err?.response?.status === 409 && detail?.code === 'pending_clash' ? detail : null;
}

/**
 * Change the signed-in person's own pending leave to the form they just filled in (#518).
 *
 * @param session - The saved sign-in.
 * @param itemId - The pending leave request to change.
 * @param form - The leave form, snake_case, as for submitting.
 * @returns The changed request's number and a fresh status token.
 * @throws AxiosError 400 bad form or nothing changed, 404 not theirs, 409 no longer pending or clashes, 503 off.
 */
export const changeMyLeave = (session: EmployeeSession, itemId: string, form: Record<string, unknown>) =>
  dashboard.post<{ status: string; request_type: string; item_id: string; status_token: string | null }>(
    `/me/requests/leave/${itemId}/change`, form, {
      params: { token: session.token, role: session.role, uid: session.uid, exp: session.exp },
    });

/**
 * Cancel the signed-in person's own pending request from the request page (#519).
 *
 * @param session - The saved sign-in.
 * @param type - "leave", "overtime" or "carryover-payout".
 * @param itemId - The request number.
 * @returns ``{status: "cancelled", item_id, already}``.
 * @throws AxiosError 404 not theirs, 409 no longer pending, 503 off.
 */
export const cancelMyRequestAs = (session: EmployeeSession, type: string, itemId: string) =>
  dashboard.post<{ status: string; item_id: string; already: boolean }>(
    `/me/requests/${type}/${itemId}/cancel`, {}, {
      params: { token: session.token, role: session.role, uid: session.uid, exp: session.exp },
    });

/**
 * Submit a request as the signed-in employee.
 *
 * @param session - The saved sign-in; sent as the usual token query parameters.
 * @param type - Which form.
 * @param form - The form body, snake_case.
 * @throws AxiosError 400 with a reason, 401 sign-in expired, 409 not routable yet, 503 off.
 */
export const submitMyRequest = (session: EmployeeSession, type: RequestType, form: Record<string, unknown>) =>
  dashboard.post<{ status: string; request_type: string; item_id: string; status_token: string | null }>(
    `/me/requests/${type}`, form, {
      params: { token: session.token, role: session.role, uid: session.uid, exp: session.exp },
    });

/** One step of a request page submission, as the status endpoint reports it. */
export interface ProgressStep {
  /** True once the step has happened. */
  done: boolean;
  /** When it happened (ISO 8601), or null while it is still waiting. */
  at: string | null;
}

/** Where a request sent from the request page stands, for the page that sent it (#364). */
export interface RequestProgress {
  /** The request's lifecycle state, lower case (pending, approved, rejected, ...). */
  status: string;
  /** The deciding manager's name, once it is known. */
  manager_name: string;
  /** True once there is nothing left to wait for (decided and the result email sent). */
  finished: boolean;
  steps: {
    /** The approval ask reached the manager. */
    sent_to_manager: ProgressStep;
    /**
     * A manager or admin approved or rejected; "result" says which once done, and
     * "decider" names who decided ("an admin", or the manager's name) once done.
     */
    decided: ProgressStep & { result: 'approved' | 'rejected' | null; decider: string | null };
    /** How the balance moved on approval; "effect" says which once done. */
    balance: ProgressStep & { effect: 'applied' | 'counted' | 'no_balance_used' | null };
    /** The decision email went to the employee. */
    result_sent: ProgressStep;
  };
}

/**
 * Ask how a request sent from the request page is going, so its "what happens
 * next" steps update on their own (#364).
 *
 * @param statusToken - The status_token the submit response returned.
 * @returns The request's progress.
 * @throws AxiosError 401 when the token is bad, 404 when the request is gone.
 */
export const getRequestStatus = (statusToken: string) =>
  intake.get<RequestProgress>('/request-status', { params: { token: statusToken } });

/** Balances in the dashboard's shape. */
export interface Balances {
  vacation_balance: number;
  vacation_entitlement: number;
  sick_balance: number;
  sick_entitlement: number;
  overtime: number;
  carryover: number;
  payout: number;
  /** True for an hourly person: Sick is their only balance (#300). */
  hourly: boolean;
  /** hourly vacation taken this calendar year (the counter); null otherwise. */
  vacation_taken: number | null;
}

/** What a request would do, from the preview endpoint. Nothing is saved. */
export interface RequestPreview {
  /** Working days (leave), days added (overtime), or days moved (carry-over/payout). */
  days: number;
  current: Balances;
  /** After approval; null when no balance changes or the request would be rejected. */
  projected: Balances | null;
  /** Why nothing changes, when that is the case. */
  unchanged: string;
  /** Things worth knowing (skipped holidays, next-year rules). */
  notes: string[];
  /** Set when the request would be rejected automatically. */
  warning: string;
  next_year: boolean;
  /** Set when approving would take the person's floor below zero (#300); never a block. */
  below_zero: BelowZero | null;
  /** The vacation booking limit for salaried vacation/part days; null otherwise (#407). */
  booking_limit: BookingLimit | null;
}

/** How far ahead a salaried person may book vacation, and whether these dates exceed it (#407). */
export interface BookingLimit {
  /** The last bookable day, as an ISO date (YYYY-MM-DD). */
  date: string;
  /** True when the request's last day runs past that limit, so it would be refused. */
  refused: boolean;
  /** The sentence to show when refused, e.g. "You can book vacation until March 31, 2027."; empty otherwise. */
  message: string;
}

/**
 * Preview what the form would do to the signed-in employee's balances.
 *
 * @param session - The saved sign-in.
 * @param type - Which form.
 * @param form - The form body, exactly as it would be submitted.
 * @returns The preview.
 * @throws AxiosError 400 for a form that is not ready, 401 sign-in expired.
 */
export const previewMyRequest = (session: EmployeeSession, type: RequestType, form: Record<string, unknown>) =>
  dashboard.post<RequestPreview>(`/me/requests/${type}/preview`, form, {
    params: { token: session.token, role: session.role, uid: session.uid, exp: session.exp },
  });

/**
 * The readable reason from a failed call.
 *
 * @param err - What axios threw.
 * @param fallback - Shown when the server gave no reason.
 * @returns A message for the person.
 */
export function errorText(err: any, fallback: string): string {
  const detail = err?.response?.data?.detail;
  return typeof detail === 'string' ? detail : fallback;
}
