import { useEffect, useRef } from 'react';

/**
 * Keep a form's typed draft in sessionStorage, so a reload restores it (#331).
 *
 * The page auto-update reloads at a safe moment; the request form's draft must
 * survive it. This hook writes the value to sessionStorage whenever it changes
 * and, once on mount, restores the last saved value if there is one. Every
 * storage call is wrapped, so a private window or blocked storage just means no
 * draft is kept, never a crash.
 *
 * @param key - A stable sessionStorage key for this form.
 * @param value - The form's current value (any JSON-serialisable shape).
 * @param restore - Called once on mount with a restored value, if one was saved.
 * @returns clearDraft, to forget the draft after a successful submit.
 */
export function useSessionDraft<T>(
  key: string,
  value: T,
  restore: (saved: T) => void,
): { clearDraft: () => void } {
  const restoreRef = useRef(restore);
  restoreRef.current = restore;

  // Restore once on mount, before the first save overwrites it.
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    try {
      const raw = sessionStorage.getItem(key);
      if (raw) restoreRef.current(JSON.parse(raw) as T);
    } catch {
      /* no draft, blocked storage, or bad JSON: start clean */
    }
  }, [key]);

  // Save on every change (after the restore has run).
  useEffect(() => {
    if (!restored.current) return;
    try {
      sessionStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* storage blocked: the draft just will not survive a reload */
    }
  }, [key, value]);

  const clearDraft = () => {
    try {
      sessionStorage.removeItem(key);
    } catch {
      /* nothing stored */
    }
  };
  return { clearDraft };
}
