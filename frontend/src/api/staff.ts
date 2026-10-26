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
  /** Why the change was made; null when nothing explains it. */
  reason: string | null;
  /** Where it came from ("admin_dashboard", "manager_dashboard", "system", "sharepoint"). */
  source: string;
  /** Who or what made it ("HR", a manager's name, "system", a SharePoint editor); null on older rows. */
  actor?: string | null;
  /** The SharePoint version label an imported row came from (e.g. "1.0"); null for an app edit. */
  version?: string | null;
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

/**
 * Save a teammate's balance and directory changes through the manager route,
 * with one reason (#432). Same payload and result as the admin save; the server
 * gates it by the manager's token and team membership and records the change
 * under the manager's name. The manager person panel uses this instead of the
 * admin route so a manager edits every field an admin can, on their team only.
 *
 * @param id - Staff Directory item id of the teammate.
 * @param payload - The pots, fields and reason.
 * @returns The save result.
 */
export const saveTeamRecord = (id: string, payload: SaveStaffPayload) =>
  api.patch<SaveStaffResult>(`/team/members/${id}/entitlements`, payload);

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

/*
 * Archive, restore and list departed employees (#119). An archived person drops
 * out of every list, team, picker, report and approval route and can no longer
 * make requests, while their past requests and history stay on record.
 */

/** One request left pending when a person was archived, for the confirmation. */
export interface ArchivePendingRequest {
  /** 'leave', 'overtime' or 'carryover-payout'. */
  request_type: string;
  /** The request item id. */
  item_id: string;
}

/** What POST /admin/staff/{id}/archive returns. */
export interface ArchiveResult {
  status: string;
  employee_id: string;
  name: string;
  /** Requests left pending on purpose, so the admin knows to handle them. */
  pending_requests: ArchivePendingRequest[];
}

/** One archived person, as GET /admin/staff/archived returns them. */
export interface ArchivedPerson {
  id: string;
  name: string;
  email: string;
  department: string;
  location: string;
  /** When they were archived, an ISO 8601 UTC string. */
  archived_at: string;
  /** Why they were archived. */
  archived_reason: string;
  /** Who archived them (the admin channel). */
  archived_by: string;
}

/**
 * Archive a departed employee, with a required reason.
 *
 * @param id - Staff Directory item id.
 * @param reason - Why they are being archived.
 * @returns The archive result, including any pending requests left behind.
 */
export const archiveStaff = (id: string, reason: string) =>
  api.post<ArchiveResult>(`/admin/staff/${id}/archive`, { reason });

/**
 * Restore an archived employee back into the Staff Directory.
 *
 * @param id - Staff Directory item id.
 * @returns The restore result ({status, employee_id, name}).
 */
export const restoreStaff = (id: string) =>
  api.post(`/admin/staff/${id}/restore`, {});

/**
 * The archived people, for the staff screen's archived list.
 *
 * @returns The axios response ({archived: ArchivedPerson[]}).
 */
export const getArchivedStaff = () =>
  api.get<{ archived: ArchivedPerson[] }>('/admin/staff/archived');
