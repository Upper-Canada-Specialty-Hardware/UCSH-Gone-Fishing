import { useEffect } from 'react';
import api from '../api/client';
import { fetchVersion } from '../api/events';

// The build this bundle was compiled from, injected by Vite's define (see
// vite.config.ts). "dev" locally, so the check does nothing in development.
declare const __APP_VERSION__: string;
const BUILD = typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : 'dev';

const CHECK_MS = 5 * 60 * 1000;   // poll /version this often, as a backstop to the header
const IDLE_MS = 30 * 1000;        // how long with no input counts as "idle"
const TRY_MS = 5 * 1000;          // how often to re-check whether it is safe to reload

/**
 * Whether the running backend build differs from this bundle's.
 *
 * Never true in development (either side is "dev"), so a local run never reloads.
 *
 * @param serverVersion - The version a response or /version reported.
 * @returns True when a newer build is live and this page is stale.
 */
function isStale(serverVersion: string): boolean {
  if (!serverVersion || serverVersion === 'dev' || BUILD === 'dev') return false;
  return serverVersion !== BUILD;
}

/**
 * Whether now is a safe moment to reload the page.
 *
 * Safe means: no dialog open, nothing editable focused, and the tab is hidden
 * or the person has been idle, so a reload never interrupts typing or reading.
 *
 * @param lastInput - When the last user input happened (epoch ms).
 * @returns True when the page may reload itself.
 */
function safeToReload(lastInput: number): boolean {
  if (document.querySelector('[role="dialog"], [aria-modal="true"]')) return false;   // a dialog is open
  const active = document.activeElement as HTMLElement | null;
  if (active) {
    const tag = active.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || active.isContentEditable) return false;
  }
  return document.hidden || Date.now() - lastInput >= IDLE_MS;   // hidden tab, or idle for a while
}

/**
 * Reload the page when a newer build is live, at a safe moment (#331).
 *
 * It learns the live version two ways: the ``X-App-Version`` header the backend
 * stamps on every response (read through a response interceptor), and a
 * ``/version`` poll every five minutes as a backstop. Typed drafts survive
 * because the request form persists them to sessionStorage as they change
 * (see useSessionDraft), so a reload restores them.
 *
 * Mount it once, near the app root.
 */
export function useVersionReload(): void {
  useEffect(() => {
    let pending = false;                                   // a newer build has been seen
    let lastInput = Date.now();

    const onInput = () => { lastInput = Date.now(); };
    const inputs = ['mousemove', 'keydown', 'pointerdown', 'scroll', 'touchstart'];
    inputs.forEach((e) => window.addEventListener(e, onInput, { passive: true }));

    /** Note a reported version; flag a reload when it is newer than this build. */
    const note = (version: unknown) => {
      if (typeof version === 'string' && isStale(version)) pending = true;
    };

    // Read the version header off every response (success or error).
    const interceptor = api.interceptors.response.use(
      (resp) => { note(resp.headers?.['x-app-version']); return resp; },
      (err) => { note(err?.response?.headers?.['x-app-version']); return Promise.reject(err); },
    );

    const check = async () => note(await fetchVersion());
    const checkTimer = setInterval(() => void check(), CHECK_MS);
    void check();                                          // one check at mount

    // When a newer build is pending, reload as soon as it is safe.
    const tryTimer = setInterval(() => {
      if (pending && safeToReload(lastInput)) window.location.reload();
    }, TRY_MS);

    return () => {
      api.interceptors.response.eject(interceptor);
      clearInterval(checkTimer);
      clearInterval(tryTimer);
      inputs.forEach((e) => window.removeEventListener(e, onInput));
    };
  }, []);
}
