/**
 * The last data each dashboard loaded, kept on this device so the next launch
 * paints at once while fresh data loads behind it.
 *
 * Stored in IndexedDB (the admin requests list is several MB, past what
 * localStorage holds). Every read and write is best effort: a private window,
 * blocked storage or a missing database just means no head start, never an
 * error. Entries are keyed by the view and who is signed in ("admin",
 * "team:manager:<id>", "me:manager:<id>", "me:employee:<id>"), so one person
 * never sees another's cached data and one view never loads another's, and
 * they are dropped when that sign-in ends.
 */

const DB_NAME = 'ucsh-launch-cache';
const STORE = 'snapshots';
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;   // older than a week: start fresh instead

/** One stored snapshot: the data and when it was saved. */
interface Entry<T> {
  saved_at: number;
  data: T;
}

/**
 * Open (creating on first use) the cache database.
 *
 * @returns The open database, or null when IndexedDB is unavailable.
 */
function openDb(): Promise<IDBDatabase | null> {
  return new Promise((resolve) => {
    try {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => request.result.createObjectStore(STORE);   // first use: one key-value store
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => resolve(null);                // blocked or unsupported: no cache
    } catch {
      resolve(null);
    }
  });
}

/**
 * Run one request against the store and close the database afterwards.
 *
 * @param mode - 'readonly' or 'readwrite'.
 * @param run - Builds the request from the store.
 * @returns The request's result, or null on any failure.
 */
async function withStore<R>(
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => IDBRequest,
): Promise<R | null> {
  const db = await openDb();
  if (!db) return null;
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(STORE, mode);
      const request = run(tx.objectStore(STORE));
      tx.oncomplete = () => { db.close(); resolve((request.result as R) ?? null); };
      tx.onerror = () => { db.close(); resolve(null); };
      tx.onabort = () => { db.close(); resolve(null); };     // e.g. quota exceeded on a write
    } catch {
      db.close();
      resolve(null);
    }
  });
}

/**
 * The snapshot saved under a key, if it is recent enough to show.
 *
 * @param key - Who it belongs to, e.g. "admin" or "manager:368".
 * @returns The saved data, or null when there is none, it is too old, or storage fails.
 */
export async function readLaunchCache<T>(key: string): Promise<T | null> {
  const entry = await withStore<Entry<T>>('readonly', (store) => store.get(key));
  if (!entry || Date.now() - entry.saved_at > MAX_AGE_MS) return null;   // missing or stale
  return entry.data;
}

/**
 * Save the data a dashboard just loaded, replacing the previous snapshot.
 *
 * @param key - Who it belongs to.
 * @param data - Plain JSON-like data (what the API returned).
 */
export async function writeLaunchCache<T>(key: string, data: T): Promise<void> {
  const entry: Entry<T> = { saved_at: Date.now(), data };
  await withStore('readwrite', (store) => store.put(entry, key));
}

/**
 * Drop saved snapshots when a sign-in ends.
 *
 * @param which - 'admin' drops the admin snapshot; 'staff' drops every
 *   manager and employee snapshot.
 */
export async function dropLaunchCache(which: 'admin' | 'staff'): Promise<void> {
  const keys = await withStore<IDBValidKey[]>('readonly', (store) => store.getAllKeys());
  for (const key of keys || []) {
    const isAdmin = key === 'admin';
    if ((which === 'admin') === isAdmin) {
      await withStore('readwrite', (store) => store.delete(key));
    }
  }
}
