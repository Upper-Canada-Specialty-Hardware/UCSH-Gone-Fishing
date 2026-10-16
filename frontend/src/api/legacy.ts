import api from './client';

/*
 * Admin legacy data handler calls (#337). The reconciliation findings the
 * Postgres move turns up: list them with filters and per-case counts, and mark
 * one resolved or ignored with a note. These ride the shared axios instance, so
 * the admin password header is attached like every other /admin call.
 */

/** What a finding is about: a request, or a staff record. */
export type FindingSubject = 'request' | 'staff';

/** Where HR has taken a finding: still open, resolved, or deliberately ignored. */
export type FindingStatus = 'open' | 'resolved' | 'ignored';

/** One reconciliation finding, as GET /admin/legacy/findings returns it. */
export interface LegacyFinding {
  /** The row id, used by the resolve and ignore calls. */
  id: number;
  subject: FindingSubject;
  /** The request kind ('leave', 'overtime', 'carryover_payout'), or null for staff. */
  kind: string | null;
  /** The request item id or the staff id, as a string. */
  subject_id: string;
  /** The named reconciliation case (see CASE_INFO on the screen). */
  case: string;
  /** One plain sentence describing the discrepancy. */
  reason: string;
  /** What backs the finding, shown as readable key and value, never raw JSON. */
  evidence: Record<string, unknown>;
  status: FindingStatus;
  /** HR's note once decided, or null. */
  note: string | null;
  /** Who resolved or ignored it, or null. */
  resolved_by: string | null;
  /** When it was decided, an ISO 8601 string, or null. */
  resolved_at: string | null;
  /** The sync run that found it, or null. */
  run_id: number | null;
  /** When it was filed, an ISO 8601 string. */
  created_at: string | null;
}

/** The page of findings plus the per-case counts for the badges. */
export interface FindingsPage {
  findings: LegacyFinding[];
  total: number;
  page: number;
  page_size: number;
  /** Every case mapped to its count at the chosen status. */
  counts: Record<string, number>;
}

/**
 * List reconciliation findings and the per-case counts for the badges.
 *
 * @param params - status (open/resolved/ignored), an optional case, an optional
 *   subject, and the 1-based page with its size.
 * @returns The axios response with the findings page.
 */
export const getLegacyFindings = (params: {
  status: FindingStatus;
  case?: string;
  subject?: FindingSubject;
  page?: number;
  page_size?: number;
}) => api.get<FindingsPage>('/admin/legacy/findings', { params });

/**
 * Mark a finding resolved: HR fixed the underlying record.
 *
 * @param id - The finding's id.
 * @param note - Why or how it was resolved; required by the backend.
 * @returns The axios response with the updated finding.
 */
export const resolveLegacyFinding = (id: number, note: string) =>
  api.post<LegacyFinding>(`/admin/legacy/findings/${id}/resolve`, { note });

/**
 * Mark a finding ignored: the discrepancy is expected or not worth fixing.
 *
 * @param id - The finding's id.
 * @param note - Why it is being ignored; required by the backend.
 * @returns The axios response with the updated finding.
 */
export const ignoreLegacyFinding = (id: number, note: string) =>
  api.post<LegacyFinding>(`/admin/legacy/findings/${id}/ignore`, { note });
