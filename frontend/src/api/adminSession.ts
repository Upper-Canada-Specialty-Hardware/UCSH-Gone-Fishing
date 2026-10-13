/**
 * The admin dashboard's own sign-in (#228), kept apart from the staff
 * sign-in. The token comes from POST /api/admin-auth/sign-in and goes back
 * as the X-Admin-Session header on every /admin call (see client.ts).
 */

const KEY = 'admin_session';                     // localStorage: lasts 30 days on this device
export const ADMIN_SIGNED_OUT = 'admin-signed-out';   // window event: the server refused the session

// Fallback when localStorage cannot be written (a private window).
let memory: AdminSession | null = null;

/** A signed-in admin session. */
export interface AdminSession {
  /** "<expiry>.<signature>", sent back unchanged. */
  token: string;
  /** Expiry in Unix seconds. */
  exp: string;
}

/**
 * This device's admin session, or null when there is none or it has expired.
 *
 * @returns The session, or null.
 */
export function loadAdminSession(): AdminSession | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const session = JSON.parse(raw) as AdminSession;
    if (!session.token || Number(session.exp) * 1000 < Date.now()) {
      localStorage.removeItem(KEY);              // expired: forget it
      return null;
    }
    return session;
  } catch {
    return null;                                 // storage blocked or unreadable: signed out
  }
}

/**
 * Remember a new session on this device.
 *
 * @param session - From sign-in or a password change.
 */
export function saveAdminSession(session: AdminSession): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    /* storage blocked: the session lasts until the page closes */
  }
  memory = session;
}

/** Forget this device's session (sign out). */
export function clearAdminSession(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* nothing stored */
  }
  memory = null;
}

/**
 * The session to send: the stored one, else the one kept in memory.
 *
 * @returns The token, or an empty string.
 */
export function adminSessionToken(): string {
  return (loadAdminSession() || memory)?.token || '';
}
