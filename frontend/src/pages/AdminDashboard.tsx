import { useEffect, useState, useCallback, useMemo } from 'react';
import {
  Box, Typography, Paper, CircularProgress, Alert, Tabs, Tab,
  Card, CardContent, Snackbar, ToggleButton, ToggleButtonGroup,
  Autocomplete, TextField, Button,
} from '@mui/material';
import Grid from '@mui/material/Grid2';
import PendingApprovals from '../components/PendingApprovals';
import TeamBalanceTable from '../components/TeamBalanceTable';
import RequestHistory from '../components/RequestHistory';
import ManagerAssignments from '../components/ManagerAssignments';
import StuckRequests from '../components/StuckRequests';
import EditRequestDialog from '../components/EditRequestDialog';
import {
  getAdminBalances,
  getAdminPending,
  getAdminRequests,
  getAdminStats,
  getAdminStuckRequests,
  getConfig,
  adminApproveRequest,
  adminRejectRequest,
  adminRefundRequest,
  adminSendReminder,
  adminReprocessRequest,
  adminEditLeaveRequest,
  adminEditOvertimeRequest,
  adminEditCarryoverPayoutRequest,
  getAdminImpersonateUrl,
  sendDashboardLink,
  getEmployeeDashboardLink,
} from '../api/client';

export default function AdminDashboard() {
  const [tab, setTab] = useState(0);
  const [employees, setEmployees] = useState<any[]>([]);
  const [pending, setPending] = useState<any[]>([]);
  const [requests, setRequests] = useState<any[]>([]);
  const [stuckRequests, setStuckRequests] = useState<any[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [processingEnabled, setProcessingEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [groupBy, setGroupBy] = useState<string | null>(null);
  const [grouped, setGrouped] = useState<Record<string, any[]> | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [snack, setSnack] = useState({ open: false, message: '', severity: 'success' as 'success' | 'error' });

  // View Employee / View Team tabs
  const [viewEmpId, setViewEmpId] = useState<string | null>(null);
  const [viewMgrId, setViewMgrId] = useState<string | null>(null);

  // Edit pending request
  const [editItem, setEditItem] = useState<any | null>(null);

  const managers = useMemo(() => {
    return employees.filter((e: any) => e.is_manager);
  }, [employees]);

  useEffect(() => {
    let cancelled = false;

    const loadData = async () => {
      while (!cancelled) {
        try {
          const [balRes, pendRes, reqRes, statsRes, stuckRes, configRes] = await Promise.all([
            getAdminBalances(),
            getAdminPending(),
            getAdminRequests(),
            getAdminStats(),
            getAdminStuckRequests(),
            getConfig(),
          ]);
          if (cancelled) return;
          setEmployees(balRes.data.employees || []);
          setPending(pendRes.data.pending || []);
          setRequests(reqRes.data.requests || []);
          setStuckRequests(stuckRes.data.stuck || []);
          setStats(statsRes.data);
          setProcessingEnabled(configRes.data.processing_enabled || false);
          setLoading(false);
          return;
        } catch {
          await new Promise((r) => setTimeout(r, 5000));
        }
      }
    };

    loadData();
    return () => { cancelled = true; };
  }, []);

  const handleGroupBy = async (_: any, value: string) => {
    setGroupBy(value || null);
    if (value) {
      const res = await getAdminBalances({ group_by: value });
      setGrouped(res.data.groups || null);
    } else {
      setGrouped(null);
    }
  };

  const handleApprove = useCallback(async (type: string, id: string) => {
    setActionLoading(`${type}-${id}`);
    try {
      await adminApproveRequest(type, id);
      setPending((prev) => prev.filter((p) => !(p.request_type === type && String(p.id) === String(id))));
      setSnack({ open: true, message: 'Request approved', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'Approval failed', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, []);

  const handleReject = useCallback(async (type: string, id: string) => {
    setActionLoading(`${type}-${id}`);
    try {
      await adminRejectRequest(type, id);
      setPending((prev) => prev.filter((p) => !(p.request_type === type && String(p.id) === String(id))));
      setSnack({ open: true, message: 'Request rejected', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'Rejection failed', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, []);

  const handleRefund = useCallback(async (type: string, id: string) => {
    setActionLoading(`${type}-${id}`);
    try {
      await adminRefundRequest(type, id);
      setRequests((prev) =>
        prev.map((r) =>
          r.request_type === type && String(r.id) === String(id)
            ? { ...r, Status: 'Refunded' }
            : r
        )
      );
      setSnack({ open: true, message: 'Request refunded', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'Refund failed', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, []);

  const handleSendReminder = useCallback(async (type: string, id: string) => {
    setActionLoading(`${type}-${id}`);
    try {
      await adminSendReminder(type, id);
      setSnack({ open: true, message: 'Reminder email sent to manager', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'Failed to send reminder', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, []);

  const handleOpenDashboard = useCallback(async (targetId: string, role: 'employee' | 'manager') => {
    try {
      const res = await getAdminImpersonateUrl(targetId, role);
      window.open(res.data.url, '_blank');
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      const message = typeof detail === 'string' ? detail : 'Failed to generate URL';
      setSnack({ open: true, message, severity: 'error' });
    }
  }, []);

  const handleSendDashboardLink = useCallback(async (targetId: string) => {
    try {
      await sendDashboardLink(targetId);
      setSnack({ open: true, message: 'Dashboard link sent', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      const message = typeof detail === 'string' ? detail : 'Failed to send dashboard link';
      setSnack({ open: true, message, severity: 'error' });
    }
  }, []);

  const handleCopyEmployeeLink = useCallback(async (targetId: string) => {
    try {
      const res = await getEmployeeDashboardLink(targetId);
      await navigator.clipboard.writeText(res.data.url);
      setSnack({ open: true, message: 'Employee dashboard link copied to clipboard', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      const message = typeof detail === 'string' ? detail : 'Failed to copy link';
      setSnack({ open: true, message, severity: 'error' });
    }
  }, []);

  const handleEditSave = useCallback(async (payload: any) => {
    if (!editItem) return;
    const id = String(editItem.id);
    const type = editItem.request_type;
    if (type === 'leave') {
      await adminEditLeaveRequest(id, payload);
    } else if (type === 'overtime') {
      await adminEditOvertimeRequest(id, payload);
    } else if (type === 'carryover-payout') {
      await adminEditCarryoverPayoutRequest(id, payload);
    } else {
      throw new Error(`Unknown request type: ${type}`);
    }
    const pendRes = await getAdminPending();
    setPending(pendRes.data.pending || []);
    setSnack({ open: true, message: 'Request updated and approval email re-sent', severity: 'success' });
  }, [editItem]);

  const handleReprocess = useCallback(async (id: string, reason: string) => {
    setActionLoading(`reprocess-${id}`);
    try {
      const res = await adminReprocessRequest(id, reason);
      const remaining = res.data.remaining_issues?.length || 0;
      // Refresh stuck + pending lists
      const [stuckRes, pendRes] = await Promise.all([
        getAdminStuckRequests(),
        getAdminPending(),
      ]);
      setStuckRequests(stuckRes.data.stuck || []);
      setPending(pendRes.data.pending || []);
      if (remaining === 0) {
        setSnack({ open: true, message: 'Request reprocessed successfully', severity: 'success' });
      } else {
        setSnack({ open: true, message: `Reprocessed with ${remaining} remaining issue(s)`, severity: 'error' });
      }
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'Reprocess failed', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, []);

  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
        <CircularProgress />
      </Box>
    );
  }

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 3, fontWeight: 600 }}>
        Admin Dashboard
      </Typography>

      {!processingEnabled && (
        <Alert severity="info" sx={{ mb: 2 }}>
          System is in reporting-only mode. Approve/reject actions are disabled.
        </Alert>
      )}

      {/* Stats summary */}
      {stats && (
        <Grid container spacing={2} sx={{ mb: 3 }}>
          <Grid size={{ xs: 12, sm: 4 }}>
            <Card>
              <CardContent>
                <Typography variant="subtitle2" color="text.secondary">Leave Requests</Typography>
                <Typography variant="h4">{stats.total_requests?.leave || 0}</Typography>
                <Typography variant="body2" color="text.secondary">
                  {stats.leave_by_status?.Pending || 0} pending
                </Typography>
              </CardContent>
            </Card>
          </Grid>
          <Grid size={{ xs: 12, sm: 4 }}>
            <Card>
              <CardContent>
                <Typography variant="subtitle2" color="text.secondary">Overtime Requests</Typography>
                <Typography variant="h4">{stats.total_requests?.overtime || 0}</Typography>
                <Typography variant="body2" color="text.secondary">
                  {stats.overtime_by_status?.Pending || 0} pending
                </Typography>
              </CardContent>
            </Card>
          </Grid>
          <Grid size={{ xs: 12, sm: 4 }}>
            <Card>
              <CardContent>
                <Typography variant="subtitle2" color="text.secondary">Carry Over / Payout</Typography>
                <Typography variant="h4">{stats.total_requests?.carryover_payout || 0}</Typography>
                <Typography variant="body2" color="text.secondary">
                  {stats.carryover_by_status?.Pending || 0} pending
                </Typography>
              </CardContent>
            </Card>
          </Grid>
        </Grid>
      )}

      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 3 }}>
        <Tab label={`Pending (${pending.length})`} />
        <Tab label="All Balances" />
        <Tab label="All Requests" />
        <Tab label="Department Summary" />
        <Tab label="View Employee" />
        <Tab label="View Team" />
        <Tab label="Manager Assignments" />
        <Tab label={`Stuck (${stuckRequests.length})`} />
      </Tabs>

      {tab === 0 && (
        <PendingApprovals
          pending={pending}
          processingEnabled={processingEnabled}
          onApprove={handleApprove}
          onReject={handleReject}
          onSendReminder={handleSendReminder}
          onEdit={(item) => setEditItem(item)}
          actionLoading={actionLoading}
        />
      )}

      {tab === 1 && (
        <Paper sx={{ p: 3 }}>
          <Box sx={{ mb: 2 }}>
            <ToggleButtonGroup value={groupBy} exclusive onChange={handleGroupBy} size="small">
              <ToggleButton value="">All</ToggleButton>
              <ToggleButton value="department">By Department</ToggleButton>
              <ToggleButton value="location">By Location</ToggleButton>
            </ToggleButtonGroup>
          </Box>
          {grouped ? (
            Object.entries(grouped).map(([group, emps]) => (
              <Box key={group} sx={{ mb: 3 }}>
                <Typography variant="h6" sx={{ mb: 1 }}>{group}</Typography>
                <TeamBalanceTable members={emps} />
              </Box>
            ))
          ) : (
            <TeamBalanceTable members={employees} />
          )}
        </Paper>
      )}

      {tab === 2 && (
        <Paper sx={{ p: 3 }}>
          <RequestHistory
            requests={requests}
            showEmployee
            onRefund={handleRefund}
            processingEnabled={processingEnabled}
            actionLoading={actionLoading}
          />
        </Paper>
      )}

      {tab === 3 && stats?.department_summary && (
        <Paper sx={{ p: 3 }}>
          <Grid container spacing={2}>
            {Object.entries(stats.department_summary).map(([dept, data]: [string, any]) => (
              <Grid key={dept} size={{ xs: 12, sm: 6, md: 4 }}>
                <Card variant="outlined">
                  <CardContent>
                    <Typography variant="h6" gutterBottom>{dept}</Typography>
                    <Typography variant="body2">Employees: {data.count}</Typography>
                    <Typography variant="body2">Avg Vacation: {data.avg_vacation}</Typography>
                    <Typography variant="body2">Avg Sick: {data.avg_sick}</Typography>
                  </CardContent>
                </Card>
              </Grid>
            ))}
          </Grid>
        </Paper>
      )}

      {tab === 4 && (
        <Paper sx={{ p: 3 }}>
          <Autocomplete
            options={employees}
            getOptionLabel={(opt: any) => `${opt.name} — ${opt.department}`}
            onChange={(_, val) => setViewEmpId(val?.id || null)}
            renderInput={(params) => <TextField {...params} label="Select Employee" />}
            sx={{ mb: 3 }}
          />
          {viewEmpId && (
            <Box sx={{ display: 'flex', gap: 2 }}>
              <Button
                variant="contained"
                onClick={() => handleOpenDashboard(viewEmpId, 'employee')}
              >
                Open Employee Dashboard
              </Button>
              <Button
                variant="outlined"
                onClick={() => handleCopyEmployeeLink(viewEmpId)}
              >
                Copy Link
              </Button>
            </Box>
          )}
        </Paper>
      )}

      {tab === 5 && (
        <Paper sx={{ p: 3 }}>
          <Autocomplete
            options={managers}
            getOptionLabel={(opt: any) => `${opt.name} — ${opt.department}`}
            onChange={(_, val) => setViewMgrId(val?.id || null)}
            renderInput={(params) => <TextField {...params} label="Select Manager" />}
            sx={{ mb: 3 }}
          />
          {viewMgrId && (
            <Box sx={{ display: 'flex', gap: 2 }}>
              <Button
                variant="contained"
                onClick={() => handleOpenDashboard(viewMgrId, 'manager')}
              >
                Open Team Dashboard
              </Button>
              <Button
                variant="outlined"
                onClick={() => handleSendDashboardLink(viewMgrId)}
              >
                Send Dashboard Link
              </Button>
              <Button
                variant="outlined"
                onClick={() => handleCopyEmployeeLink(viewMgrId)}
              >
                Copy Employee Link
              </Button>
            </Box>
          )}
        </Paper>
      )}

      {tab === 6 && <ManagerAssignments />}

      {tab === 7 && (
        <Paper sx={{ p: 3 }}>
          <StuckRequests
            stuckRequests={stuckRequests}
            processingEnabled={processingEnabled}
            onReprocess={handleReprocess}
            actionLoading={actionLoading}
          />
        </Paper>
      )}

      <Snackbar
        open={snack.open}
        autoHideDuration={4000}
        onClose={() => setSnack((s) => ({ ...s, open: false }))}
        message={snack.message}
      />

      <EditRequestDialog
        open={editItem !== null}
        item={editItem}
        onClose={() => setEditItem(null)}
        onSave={handleEditSave}
      />
    </Box>
  );
}
