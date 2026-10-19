import axios from 'axios';
import { clearSession, loadSession, setSignInNotice } from './intake';
import { ADMIN_SIGNED_OUT, AdminSession, adminSessionToken, clearAdminSession } from './adminSession';

const API_URL = import.meta.env.VITE_API_URL || '';

const api = axios.create({
  baseURL: `${API_URL}/api/dashboard`,
});

// Every call carries the one shared sign-in: an emailed link in this tab, or
// this device's 30-day sign-in from the emailed code (see loadSession).
api.interceptors.request.use((config) => {
  const session = loadSession();
  if (session) {
    const { token, role, uid, exp } = session;
    config.params = { ...config.params, token, role, uid, exp };
  }
  // Admin calls also carry the admin dashboard's own sign-in (#228).
  if (config.url?.startsWith('/admin')) {
    config.headers.set('X-Admin-Session', adminSessionToken());
  }
  return config;
});

// A refused sign-in (expired or tampered) signs out and goes back to the
// landing page, which says why.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    // A refused admin call means the admin session ended (expired, or the
    // password changed): back to the password screen, staff sign-in untouched.
    if (error.response?.status === 401 && error.config?.url?.startsWith('/admin')) {
      clearAdminSession();
      window.dispatchEvent(new Event(ADMIN_SIGNED_OUT));
      return Promise.reject(error);
    }
    if (error.response?.status === 401) {
      clearSession();
      setSignInNotice('Your sign-in has ended. Enter your email for a new code.');
      window.location.hash = '#/';
    }
    return Promise.reject(error);
  }
);

export default api;

// Admin password (#228)
/**
 * Sign in to the admin dashboard. Public, so it skips the dashboard client.
 *
 * @param password - What was typed.
 * @returns The new session.
 * @throws The axios error: 401 wrong, 403 none set, 429 too many tries.
 */
export const adminSignIn = (password: string) =>
  axios.post<AdminSession>(`${API_URL}/api/admin-auth/sign-in`, { password });
/**
 * Change the admin password; other devices are signed out.
 *
 * @param current - The password now.
 * @param next - The new one.
 * @returns A fresh session for this device.
 */
export const changeAdminPassword = (current: string, next: string) =>
  api.put<AdminSession>('/admin/password', { current, new: next });

// Config
export const getConfig = () => api.get('/config');

// Employee
export const getMyBalances = () => api.get('/me/balances');
export const getMyRequests = (params?: Record<string, string>) =>
  api.get('/me/requests', { params });

// Manager
export const getTeamMembers = () => api.get('/team/members');
export const getTeamBalances = () => api.get('/team/balances');
export const getTeamPending = () => api.get('/team/pending');
export const getTeamRequests = (params?: Record<string, string>) =>
  api.get('/team/requests', { params });
export const getTeamCalendar = (params?: Record<string, string>) =>
  api.get('/team/calendar', { params });
export const approveRequest = (type: string, id: string) =>
  api.post(`/team/approve/${type}/${id}`);
export const rejectRequest = (type: string, id: string) =>
  api.post(`/team/reject/${type}/${id}`);

// Manager - log a sick day for a team member; the backend creates the "Sick or
// Personal Day" request in their name and approves it on the spot. Offered only
// where GET /team/members reports can_log_sick_days (an enabled province).
export const logSickDay = (payload: {
  employee_id: string; start_date: string; end_date?: string; notes?: string;
}) => api.post('/team/sick-days', payload);

// Admin
export const getAdminBalances = (params?: Record<string, string>) =>
  api.get('/admin/balances', { params });
export const getAdminRequests = (params?: Record<string, string>) =>
  api.get('/admin/requests', { params });
export const getAdminPending = () => api.get('/admin/pending');
export const getAdminStats = () => api.get('/admin/stats');

export const adminApproveRequest = (type: string, id: string) =>
  api.post(`/admin/approve/${type}/${id}`);
export const adminRejectRequest = (type: string, id: string) =>
  api.post(`/admin/reject/${type}/${id}`);

export const adminSendReminder = (type: string, id: string) =>
  api.post(`/admin/send-reminder/${type}/${id}`);

export const adminRefundRequest = (type: string, id: string) =>
  api.post(`/admin/refund/${type}/${id}`);

// Admin impersonation
export const getAdminImpersonateUrl = (targetId: string, role: 'employee' | 'manager') =>
  api.get('/admin/impersonate-url', { params: { target_id: targetId, target_role: role } });

export const sendDashboardLink = (targetId: string) =>
  api.post(`/admin/send-dashboard-link/${targetId}`);

export const getEmployeeDashboardLink = (targetId: string) =>
  api.get(`/admin/employee-dashboard-link/${targetId}`);

// Admin — Manager Assignments
export const getManagerAssignments = () => api.get('/admin/manager-assignments');
export const getSpUsers = () => api.get('/admin/sp-users');
export const updateManagerAssignment = (employeeId: string, managerIds: number[]) =>
  api.patch(`/admin/manager-assignments/${employeeId}`, { manager_ids: managerIds });
export const bulkManagerAssignment = (params: {
  operation: 'replace' | 'add' | 'remove';
  preview?: boolean;
  source_manager_id?: number;
  target_manager_id?: number;
  employee_ids?: string[];
}) => api.post('/admin/manager-assignments/bulk', params);

// Admin — Validate employee setup (read-only; runs the current Staff Directory
// values through every workflow/leave type without creating a request)
export const validateEmployee = (id: string) =>
  api.get(`/admin/validate-employee/${id}`);

// The same checks across the whole Staff Directory, so a record that would
// stall a request is listed before anyone is blocked by it.
export const getAdminEmployeeSetup = () => api.get('/admin/employee-setup');

// Manager — create a new employee (Staff Directory record) for their team
export const createEmployee = (payload: Record<string, unknown>) =>
  api.post('/team/employees', payload);

// Admin — create a new employee, choosing the supervisor(s) via manager_ids
export const createEmployeeAdmin = (payload: Record<string, unknown>) =>
  api.post('/admin/employees', payload);

// Admin - Company Holidays editor (the dashboard edit surface for HR)
export const getAdminHolidays = () => api.get('/admin/holidays');
export const createHoliday = (payload: { title: string; date: string; province: string }) =>
  api.post('/admin/holidays', payload);
export const updateHoliday = (id: string, payload: { title: string; date: string; province: string }) =>
  api.patch(`/admin/holidays/${id}`, payload);
export const deleteHoliday = (id: string) => api.delete(`/admin/holidays/${id}`);

// Admin - holidays SharePoint -> Postgres copy + parity check

// Admin — Stuck Requests / Reprocess
export const getAdminStuckRequests = () => api.get('/admin/stuck-requests');
export const adminReprocessRequest = (id: string, reason: string) =>
  api.post(`/admin/reprocess/leave/${id}`, { reason });

// Admin — Edit pending requests
export const adminEditLeaveRequest = (id: string, payload: {
  Days: number; LeaveType: string; StartDate: string; EndDate: string; reason: string;
}) => api.post(`/admin/edit/leave/${id}`, payload);

export const adminEditOvertimeRequest = (id: string, payload: {
  Hours: number; StartDate: string; Title: string; reason: string;
}) => api.post(`/admin/edit/overtime/${id}`, payload);

export const adminEditCarryoverPayoutRequest = (id: string, payload: {
  TypeofRequest: string; Days: number; reason: string;
}) => api.post(`/admin/edit/carryover-payout/${id}`, payload);

// Admin - Email Log: every SMTP2GO request/response that named one person.
// The backend resolves the address from the Staff Directory when only an
// employee_id is given; `days` bounds the window (default 30 server-side).
export const getAdminEmailLog = (params: { employee_id?: string; address?: string; days?: number }) =>
  api.get('/admin/email-log', { params });

// Admin - Email Log: every recent email through any service (SMTP2GO or the
// UCSH mailer), no person needed. `hours` bounds the window, `limit` caps rows,
// `text` narrows to a recipient/subject substring (server-side; the UI also
// filters the returned set as you type).
export const getAdminEmailLogRecent = (
  params: { hours?: number; limit?: number; text?: string },
) => api.get('/admin/email-log/recent', { params });

// Admin - Email Log: send a labelled test email to one person through the
// normal send path and get that send's log row back.
export const sendAdminTestEmail = (payload: { employee_id?: string; address?: string }) =>
  api.post('/admin/email-log/test', payload);

// Admin - Held Requests: request page submissions from people not in the Staff
// Directory yet, waiting for their supervisor to add them.
export const getHeldRequests = (includeClosed = false) =>
  api.get('/admin/held-requests', { params: { include_closed: includeClosed } });
export const releaseHeldRequest = (id: number) => api.post(`/admin/held-requests/${id}/release`);
export const cancelHeldRequest = (id: number) => api.post(`/admin/held-requests/${id}/cancel`);

// Admin - the SubmitterEmail and RequestSource columns the request page writes.
export const getRequestColumns = () => api.get('/admin/request-columns');
export const addRequestColumns = () => api.post('/admin/request-columns');
// Admin - where requests and staff live, and the one-step switch to Postgres.
/** Current storage, the last sync and recent runs, request counts and open findings. */
export const getStorageStatus = () => api.get('/admin/storage');
/** Run the final sync and switch requests (and staff) to Postgres. One-way. */
export const flipStorageToPostgres = () => api.post('/admin/storage/flip');
/** The dates carry-over and payout requests are accepted between (Toronto dates, both ends included). */
export interface CarryoverWindow {
  start: string | null;   // YYYY-MM-DD, or null when no dates are set
  end: string | null;
  open: boolean;          // whether today is inside the dates
  today: string;          // the Toronto date the server checked
}
// Admin - the carry-over and payout dates (none set means closed), and the Excel export
export const getCarryoverPayoutWindow = () => api.get<CarryoverWindow>('/admin/carryover-payout');
export const setCarryoverPayoutWindow = (start: string | null, end: string | null) =>
  api.put<CarryoverWindow>('/admin/carryover-payout', { start, end });
export const exportCarryoverPayout = () =>
  api.get<Blob>('/admin/carryover-payout/export', { responseType: 'blob' });

// #360 - manager History tab: download the team requests on screen as an .xlsx.
// The route is a /team path, so the dashboard sign-in token (added by the
// interceptor above) authorises it; it is scoped to the manager's team by the
// data the client sends, which only ever comes from /team/requests.
/**
 * Download the team requests the manager is viewing as an .xlsx (one sheet per list).
 *
 * @param requests - The request items as /team/requests returns them.
 * @returns The axios response, with a Blob body.
 */
export const exportTeamRequests = (requests: any[]) =>
  api.post<Blob>('/team/requests/export', { requests }, { responseType: 'blob' });
// Admin - #370: pending requests whose managers are all on vacation, so nobody
// can approve them until a manager is back. The admin approves/rejects them here
// with the usual admin approve/reject calls above.
/** One manager of a stuck request, and the date they are back. */
export interface VacationCoverManager {
  name: string;
  back: string;   // "Nov 10, 2026"
}
/** A pending request no manager can approve because all its managers are off. */
export interface VacationCoverRequest {
  request_type: string;   // "leave" / "overtime" / "carryover-payout", for the action calls
  id: string;
  employee_name: string;
  what: string;           // the leave type, overtime hours, or carry-over/payout word
  when: string;           // the dates, or the day count for carry-over/payout
  managers: VacationCoverManager[];
}
export const getAdminVacationCover = () =>
  api.get<{ requests: VacationCoverRequest[] }>('/admin/vacation-cover');

// Manager — control over their own team (#362, #363). A manager edits a
// teammate's balances/entitlements with a reason, and adds or removes people on
// their team. Every change is recorded under the manager's name server-side.

/** One addable person the picker lists. */
export interface AddableMember {
  id: string;
  name: string;
  email: string;
  department: string;
  location: string;
  employment_type: string;
  system_check: string;
}

/**
 * Change one teammate's balances and entitlements, with a required reason (#362).
 *
 * @param employeeId - Staff Directory id of the teammate.
 * @param payload - The balance-pot wire keys to change and the reason.
 * @returns The save result (what changed, whether history was recorded).
 */
export const updateTeamEntitlements = (
  employeeId: string, payload: { changes: Record<string, number>; reason: string },
) => api.patch(`/team/members/${employeeId}/entitlements`, payload);

/**
 * Staff the manager may still add (everyone not already on their team) (#363).
 *
 * @returns ``{ people: AddableMember[] }`` in name order.
 */
export const getAddableMembers = () =>
  api.get<{ people: AddableMember[] }>('/team/addable');

/**
 * One teammate's managers and whether anyone else would remain (#363). Read
 * before the remove confirmation so the UI can warn about an orphaned person.
 *
 * @param employeeId - Staff Directory id of the teammate.
 * @returns ``{ id, name, managers, has_other_manager }``.
 */
export const getTeamMemberManagers = (employeeId: string) =>
  api.get<{ id: string; name: string; managers: string[]; has_other_manager: boolean }>(
    `/team/members/${employeeId}/managers`,
  );

/**
 * Add a person to the calling manager's team (#363).
 *
 * @param employeeId - Staff Directory id of the person to add.
 * @returns ``{ id, name, managers, added }``.
 */
export const addTeamMember = (employeeId: string) =>
  api.post(`/team/members/${employeeId}`);

/**
 * Remove the calling manager from a teammate, leaving any other managers (#363).
 *
 * @param employeeId - Staff Directory id of the teammate.
 * @returns ``{ id, name, managers, removed, has_other_manager }``.
 */
export const removeTeamMember = (employeeId: string) =>
  api.delete(`/team/members/${employeeId}`);
