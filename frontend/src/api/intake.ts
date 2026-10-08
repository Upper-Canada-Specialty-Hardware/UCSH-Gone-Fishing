import axios from 'axios';

const API_URL = import.meta.env.VITE_API_URL || '';

/**
 * Calls for the public request page. Kept apart from the dashboard client
 * (./client.ts) on purpose: that client reads its token from sessionStorage
 * and sends any 401 to the "link expired" page, while the request page keeps
 * its own 30-day sign-in in localStorage and handles an expired one itself
 * (by asking for a new code).
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

/** The token /verify returns for an address on no staff record. */
export interface VerifiedEmail {
  email: string;
  exp: string;
  token: string;
}

/** One supervisor a new hire can pick. */
export interface Supervisor {
  id: string;
  name: string;
  location: string;
}

export type RequestType = 'leave' | 'overtime' | 'carryover-payout';

const SESSION_KEY = 'request_page_session';   // localStorage key for the 30-day sign-in
const HANDOFF_KEY = 'request_page_handoff';   // sessionStorage key for a sign-in handed over by the employee dashboard

/**
 * One stored sign-in, if present and not expired; an expired one is removed.
 *
 * @param store - localStorage (the 30-day sign-in) or sessionStorage (a dashboard handoff).
 * @param key - The key it is kept under.
 * @returns The session, or null.
 */
function readSession(store: Storage, key: string): EmployeeSession | null {
  const raw = store.getItem(key);
  if (!raw) return null;
  const session = JSON.parse(raw) as EmployeeSession;
  if (Number(session.exp) * 1000 <= Date.now()) {      // exp is unix seconds
    store.removeItem(key);
    return null;
  }
  return session;
}

/**
 * The sign-in to use, if any and not expired: one handed over by the employee
 * dashboard in this tab first, then this device's 30-day sign-in.
 *
 * @returns The session, or null when there is none, it expired, or storage is blocked.
 */
export function loadSession(): EmployeeSession | null {
  try {
    return readSession(sessionStorage, HANDOFF_KEY) ?? readSession(localStorage, SESSION_KEY);
  } catch {
    return null;                                       // private window or blocked storage
  }
}

/**
 * Hand the employee dashboard's sign-in to the request page, for this browser
 * tab only. It is the same signed employee token, so the request page skips
 * the email code. It is deliberately not saved for 30 days: an admin who opens
 * someone's dashboard and follows "Make a request" must not stay signed in as
 * that person on their own device.
 *
 * @param session - Built from the dashboard's token and the employee's name and email.
 */
export function handOffSession(session: EmployeeSession): void {
  try {
    sessionStorage.setItem(HANDOFF_KEY, JSON.stringify(session));
  } catch {
    /* storage blocked: the request page asks for a code instead */
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

/** Forget the sign-in ("Not you? Sign out"). */
export function clearSession(): void {
  try {
    localStorage.removeItem(SESSION_KEY);
    sessionStorage.removeItem(HANDOFF_KEY);            // a dashboard handoff too
  } catch {
    /* nothing to clear */
  }
}

/**
 * The employee's own dashboard link, built from the saved sign-in.
 *
 * @param session - The saved sign-in.
 * @returns A hash URL the existing dashboard sign-in handler accepts.
 */
export function dashboardHref(session: EmployeeSession): string {
  const params = new URLSearchParams({
    token: session.token, role: session.role, uid: session.uid, exp: session.exp,
  });
  return `#/dashboard?${params.toString()}`;
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
 * @returns {status: "employee", name, role, uid, token, exp} or {status: "unknown", verified}.
 * @throws AxiosError 400 with a readable reason when the code is wrong or expired.
 */
export const verifyCode = (email: string, code: string) =>
  intake.post('/verify', { email, code });

/**
 * The supervisors and locations a new hire can choose from.
 *
 * @param verified - The token from verifyCode.
 * @throws AxiosError 401 when the token expired.
 */
export const getSupervisors = (verified: VerifiedEmail) =>
  intake.get('/supervisors', { params: verified });

/**
 * Hold a request from someone not on staff until their supervisor adds them.
 *
 * @param body - verified token, name, location, supervisor_id, request_type, form.
 * @throws AxiosError 400 with a reason, 401 expired, 409 already on staff.
 */
export const holdRequest = (body: {
  verified: VerifiedEmail;
  name: string;
  location: string;
  supervisor_id: string;
  request_type: RequestType;
  form: Record<string, unknown>;
}) => intake.post('/held', body);

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
