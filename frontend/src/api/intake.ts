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

/** The token /verify returns for an address on no staff record. */
export interface VerifiedEmail {
  email: string;
  exp: string;
  token: string;
}

/** One person a new starter can pick as their supervisor. */
export interface Person {
  id: string;
  name: string;
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
const NEW_HIRE_KEY = 'new_hire_verified';      // sessionStorage: a new hire's verified-email token

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
 * Keep a new hire's verified-email token for this tab, so /new-hire survives a refresh.
 *
 * @param verified - From verifyCode, or null to forget it.
 */
export const saveNewHire = (verified: VerifiedEmail | null) =>
  tabSet(NEW_HIRE_KEY, verified ? JSON.stringify(verified) : null);

/**
 * The new hire's verified-email token for this tab, if it has not expired (24 hours).
 *
 * @returns The token, or null.
 */
export function loadNewHire(): VerifiedEmail | null {
  try {
    const raw = sessionStorage.getItem(NEW_HIRE_KEY);
    const verified = raw ? (JSON.parse(raw) as VerifiedEmail) : null;
    return verified && !expired(verified.exp) ? verified : null;
  } catch {
    return null;
  }
}

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
 * @returns {status: "employee", name, role, uid, token, exp}; {status: "choose", people}
 *   when several staff records share the address; or {status: "unknown", verified}.
 * @throws AxiosError 400 with a readable reason when the code is wrong or expired.
 */
export const verifyCode = (email: string, code: string) =>
  intake.post('/verify', { email, code });

/**
 * The departments a new starter can choose from, as the Staff Directory spells them.
 *
 * @param verified - The token from verifyCode.
 * @throws AxiosError 401 when the token expired.
 */
export const getDepartments = (verified: VerifiedEmail) =>
  intake.get<{ departments: string[] }>('/departments', { params: verified });

/**
 * People in a department whose name contains the typed text (a few at most).
 *
 * @param verified - The token from verifyCode.
 * @param department - The department picked.
 * @param q - What was typed; nobody comes back for fewer than two letters.
 * @throws AxiosError 401 when the token expired.
 */
export const searchSupervisors = (verified: VerifiedEmail, department: string, q: string) =>
  intake.get<{ people: Person[] }>('/supervisor-search', { params: { ...verified, department, q } });

/**
 * Hold a request from someone not on staff until their supervisor adds them.
 *
 * @param body - verified token, name, department, the supervisor (supervisor_id
 *   from the search, or supervisor_email), request_type and form.
 * @throws AxiosError 400 with a reason, 401 expired, 409 already on staff.
 */
export const holdRequest = (body: {
  verified: VerifiedEmail;
  first_name: string;
  last_name: string;
  department: string;
  supervisor_id?: string;
  supervisor_email?: string;
  request_type: RequestType;
  form: Record<string, unknown>;
}) => intake.post<{ status: 'held'; supervisor_name: string; status_token: string }>('/held', body);

/** Where a held request stands, as the waiting page shows it. */
export type HeldStatus = 'waiting' | 'sent' | 'cancelled';

/**
 * Ask how a held request is going, so the waiting page can update on its own.
 *
 * @param statusToken - The status_token holdRequest returned.
 * @returns The status and the supervisor it waits on.
 * @throws AxiosError 404 when the token is bad or the request is gone.
 */
export const getHeldStatus = (statusToken: string) =>
  intake.get<{ status: HeldStatus; supervisor_name: string }>('/held/status', { params: { token: statusToken } });

/**
 * Submit a request as the signed-in employee.
 *
 * @param session - The saved sign-in; sent as the usual token query parameters.
 * @param type - Which form.
 * @param form - The form body, snake_case.
 * @throws AxiosError 400 with a reason, 401 sign-in expired, 409 not routable yet, 503 off.
 */
export const submitMyRequest = (session: EmployeeSession, type: RequestType, form: Record<string, unknown>) =>
  dashboard.post(`/me/requests/${type}`, form, {
    params: { token: session.token, role: session.role, uid: session.uid, exp: session.exp },
  });

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
