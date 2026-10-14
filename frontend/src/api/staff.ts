import api from './client';

/*
 * Admin person-panel calls (#273, #276, #277). These sit apart from client.ts
 * so the staff screen owns them; they ride the same axios instance, so the
 * admin password header and the shared sign-in are attached as usual.
 */

/** One change row in a person's history, as the backend records it. */
export interface StaffChange {
  /** The SharePoint column that changed, e.g. "CurrentVacationBalance". */
  field: string;
  /** Value before the change (number, string or null). */
  old_value: unknown;
  /** Value after the change. */
  new_value: unknown;
  /** Why HR made the change. */
  reason: string;
  /** Where it came from ("admin_dashboard"). */
  source: string;
  /** When it happened, an ISO 8601 UTC string. */
  changed_at: string;
}

/** One manager on a record: their SharePoint user id and display name. */
export interface StaffManager {
  sp_user_id: number;
  name: string;
}

/** The full person record the panel shows, as GET /admin/employees/{id} returns. */
export interface StaffRecord {
  /** Staff Directory item id. */
  employee_id: string;
  /** The panel's Staff Directory columns (only those present on the record). */
  fields: Record<string, string | number>;
  /** Resolved people fields: the two links by name, and the managers as objects. */
  people: { title_link: string; supervisor_link: string; managers: StaffManager[] };
  /** The change history, newest first. */
  history: StaffChange[];
}

/**
 * Read one person's record for the panel.
 *
 * @param id - Staff Directory item id.
 * @returns The record payload.
 */
export const getStaffRecord = (id: string) => api.get<StaffRecord>(`/admin/employees/${id}`);

/** A person link's new value: the chosen SharePoint user, or null to clear. */
export interface PersonLinkValue {
  sp_user_id: number | null;
  name: string;
}

/** The body of a save: the pots, the directory fields, and the required reason. */
export interface SaveStaffPayload {
  /** Balance-pot wire key -> new number (only the ones that changed). */
  changes?: Record<string, number>;
  /** Directory-field wire key -> new value (text, a person link, or managers). */
  fields?: Record<string, unknown>;
  /** Why HR made the change; required by the backend. */
  reason: string;
}

/** What a save returns: what changed and whether the history was recorded. */
export interface SaveStaffResult {
  employee_id: string;
  name: string;
  changed: { field: string; before: unknown; after: unknown }[];
  history_recorded: boolean;
  notice?: string;
}

/**
 * Save a person's balance and directory changes together, with one reason.
 *
 * @param id - Staff Directory item id.
 * @param payload - The pots, fields and reason.
 * @returns The save result.
 */
export const saveStaffRecord = (id: string, payload: SaveStaffPayload) =>
  api.patch<SaveStaffResult>(`/admin/employees/${id}/entitlements`, payload);

/** What the export dialog sends: the people, columns and extra sheets. */
export interface StaffExportPayload {
  /** The shown people's ids, in the table's order. An empty list means nobody;
   *  null (omitted) means everyone, in name order. */
  employee_ids: string[] | null;
  /** The chosen column keys (name is always included server-side). */
  columns: string[];
  /** Add the per-department summary sheet. */
  include_summary: boolean;
  /** Add the change-history sheet. */
  include_history: boolean;
}

/**
 * Download the staff table as an .xlsx file.
 *
 * @param payload - The people, columns and sheets to include.
 * @returns The workbook as a blob response.
 */
export const exportStaff = (payload: StaffExportPayload) =>
  api.post<Blob>('/admin/staff/export', payload, { responseType: 'blob' });
