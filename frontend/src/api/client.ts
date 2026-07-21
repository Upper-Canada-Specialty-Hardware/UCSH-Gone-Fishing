import axios from 'axios';

const API_URL = import.meta.env.VITE_API_URL || '';

const api = axios.create({
  baseURL: `${API_URL}/api/dashboard`,
});

// Attach auth params from sessionStorage to every request
api.interceptors.request.use((config) => {
  const token = sessionStorage.getItem('dashboard_token');
  const role = sessionStorage.getItem('dashboard_role');
  const uid = sessionStorage.getItem('dashboard_uid');
  const exp = sessionStorage.getItem('dashboard_exp');

  if (token && role && uid && exp) {
    config.params = {
      ...config.params,
      token,
      role,
      uid,
      exp,
    };
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      sessionStorage.clear();
      window.location.hash = '#/expired';
    }
    return Promise.reject(error);
  }
);

export default api;

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
