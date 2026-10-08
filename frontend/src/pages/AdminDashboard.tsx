import { useEffect, useState, useCallback, useMemo } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Box, Typography, Paper, CircularProgress, Alert,
  Card, CardContent, Snackbar, ToggleButton, ToggleButtonGroup,
  Autocomplete, TextField, Button,
} from '@mui/material';
import Grid from '@mui/material/Grid2';
import TopBar from '../components/TopBar';
import AdminNav, { NavGroup } from '../components/admin/AdminNav';
import AdminHome from '../components/admin/AdminHome';
import TeamBalanceTable from '../components/TeamBalanceTable';
import ManagerAssignments from '../components/ManagerAssignments';
import EmployeeValidation from '../components/EmployeeValidation';
import { EmployeeSetupSummary } from '../components/EmployeeSetupList';
import EditRequestDialog from '../components/EditRequestDialog';
import AddEmployee, { ManagerOption } from '../components/AddEmployee';
import EmailLog from '../components/EmailLog';
import RequestsScreen from '../components/admin/RequestsScreen';
import RequestColumnsCard from '../components/admin/RequestColumnsCard';
import { HeldRow, OPEN_HELD, RequestView } from '../components/admin/requestRows';
import {
  getAdminBalances,
  getAdminPending,
  getAdminRequests,
  getAdminStats,
  getAdminStuckRequests,
  getAdminEmployeeSetup,
  getConfig,
  getSpUsers,
  createEmployeeAdmin,
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
  getHeldRequests,
  releaseHeldRequest,
  cancelHeldRequest,
} from '../api/client';

/** Every admin screen, keyed by its url segment (#/admin/<key>). */
const SCREEN_TITLES: Record<string, string> = {
  home: 'Home',
  pending: 'Pending approvals',
  held: 'Held for new hires',
  setup: 'Employee setup',
  add: 'Add employee',
  balances: 'All balances',
  departments: 'Department summary',
  'view-employee': 'View employee',
  'view-team': 'View team',
  assignments: 'Manager assignments',
  requests: 'All requests',
  stuck: 'Stuck requests',
  emails: 'Email log',
  checks: 'Data checks',
};

/** The four screens that are views of the one requests table. */
const REQUEST_VIEWS: Record<string, RequestView> = { pending: 'pending', held: 'held', stuck: 'stuck', requests: 'all' };
/** A view back to its screen key, for the view chips. */
const VIEW_SCREEN: Record<RequestView, string> = { pending: 'pending', held: 'held', stuck: 'stuck', all: 'requests' };

/**
 * The admin dashboard: a grouped sidebar instead of a tab strip, a home screen
 * of what needs attention, and the open screen in the url so it survives a
 * refresh and can be bookmarked or shared.
 *
 * @returns The admin dashboard.
 */
export default function AdminDashboard() {
  const params = useParams();
  const navigate = useNavigate();
  // The screen comes from the url; anything unknown falls back to home.
  const tab = params.screen && SCREEN_TITLES[params.screen] ? params.screen : 'home';
  /** Open a screen (changes the url). @param key - Screen key. */
  const setTab = useCallback((key: string) => {
    navigate(`/admin/${key}`);                         // the url is the source of truth
    window.scrollTo(0, 0);                             // a new screen starts at the top
  }, [navigate]);
  const [menuOpen, setMenuOpen] = useState(false);     // small screens: the slide-out menu
  const [heldRows, setHeldRows] = useState<HeldRow[]>([]);         // held requests (open, or all with the switch on)
  const [heldIncludeClosed, setHeldIncludeClosed] = useState(false);
  // Open held requests, for home and the sidebar, whatever the switch shows.
  const heldCount = heldRows.filter((h) => OPEN_HELD.includes(h.status)).length;
  const [employees, setEmployees] = useState<any[]>([]);
  const [pending, setPending] = useState<any[]>([]);
  const [requests, setRequests] = useState<any[]>([]);
  const [stuckRequests, setStuckRequests] = useState<any[]>([]);
  const [employeeSetup, setEmployeeSetup] = useState<EmployeeSetupSummary | null>(null);
  const [setupRefreshing, setSetupRefreshing] = useState(false);
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

  // A held request's person, to prefill Add Employee from the Held Requests tab.
  const [addPrefill, setAddPrefill] = useState<
    { title: string; email_address: string; location: string } | null
  >(null);
  const handleAddFromHeld = useCallback((row: HeldRow) => {
    setAddPrefill({ title: row.name, email_address: row.email, location: row.location });
    setTab('add');                                     // the Add Employee screen
  }, [setTab]);

  const managers = useMemo(() => {
    return employees.filter((e: any) => e.is_manager);
  }, [employees]);

  // The supervisor picker for Add Employee. Fetched once, lazily, when that tab
  // is first opened — not on every poll of loadData, since it is a full staff
  // read and the dashboard's data loop runs continuously.
  const [spUsers, setSpUsers] = useState<ManagerOption[]>([]);
  useEffect(() => {
    if (tab !== 'add' || spUsers.length > 0) return;
    getSpUsers()
      .then((r) => setSpUsers(
        (r.data.users || []).map((u: any) => ({ sp_user_id: u.sp_user_id, name: u.name })),
      ))
      .catch(() => { /* the form still submits; the picker is just empty */ });
  }, [tab, spUsers.length]);

  useEffect(() => {
    let cancelled = false;

    const loadData = async () => {
      while (!cancelled) {
        try {
          const [balRes, pendRes, reqRes, statsRes, stuckRes, setupRes, configRes, heldRes] = await Promise.all([
            getAdminBalances(),
            getAdminPending(),
            getAdminRequests(),
            getAdminStats(),
            getAdminStuckRequests(),
            getAdminEmployeeSetup(),
            getConfig(),
            // A failure here must not hold up the dashboard; the held view just shows empty.
            getHeldRequests(false).catch(() => ({ data: { held: [] } })),
          ]);
          if (cancelled) return;
          setEmployees(balRes.data.employees || []);
          setPending(pendRes.data.pending || []);
          setRequests(reqRes.data.requests || []);
          setStuckRequests(stuckRes.data.stuck || []);
          setEmployeeSetup(setupRes.data);
          setStats(statsRes.data);
          setProcessingEnabled(configRes.data.processing_enabled || false);
          setHeldRows(heldRes.data.held || []);
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

  // Re-runs the directory-wide setup check on its own. It is the one list an
  // admin refreshes after fixing a record, and it is the expensive read of the
  // set, so it is not folded into the dashboard's other refreshes.
  const refreshEmployeeSetup = useCallback(async () => {
    setSetupRefreshing(true);
    try {
      const res = await getAdminEmployeeSetup();
      setEmployeeSetup(res.data);
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({
        open: true,
        message: typeof detail === 'string' ? detail : 'Could not refresh the setup check',
        severity: 'error',
      });
    } finally {
      setSetupRefreshing(false);
    }
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

  /**
   * Reload the held list.
   *
   * @param includeClosed - Also show sent and cancelled ones.
   */
  const loadHeld = useCallback(async (includeClosed: boolean) => {
    try {
      const res = await getHeldRequests(includeClosed);
      setHeldRows(res.data.held || []);
    } catch {
      setSnack({ open: true, message: 'Held requests could not be loaded', severity: 'error' });
    }
  }, []);

  /** Show or hide sent and cancelled held requests. @param v - Show them. */
  const handleHeldIncludeClosed = useCallback((v: boolean) => {
    setHeldIncludeClosed(v);
    loadHeld(v);
  }, [loadHeld]);

  /**
   * Run one held-request action, then reload the held list.
   *
   * @param id - The held request.
   * @param action - The call to make.
   * @param done - What to say when it worked.
   */
  const heldAction = useCallback(async (id: number, action: () => Promise<unknown>, done: string) => {
    setActionLoading(`held-${id}`);                    // the busy key the requests table looks for
    try {
      await action();
      setSnack({ open: true, message: done, severity: 'success' });
      await loadHeld(heldIncludeClosed);
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'That did not work', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, [loadHeld, heldIncludeClosed]);

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

  // The sidebar, grouped the way HR thinks about the work. The last group has no
  // heading and sits under a divider: the technical screens, unlabelled.
  const setupCount = employeeSetup?.flagged?.length ?? 0;
  const groups: NavGroup[] = [
    { items: [{ key: 'home', label: 'Home' }] },
    { title: 'Approvals', items: [
      { key: 'pending', label: 'Pending approvals', count: pending.length },
      { key: 'held', label: 'Held for new hires', count: heldCount },
    ] },
    { title: 'People', items: [
      { key: 'setup', label: 'Employee setup', count: setupCount },
      { key: 'add', label: 'Add employee' },
      { key: 'balances', label: 'All balances' },
      { key: 'departments', label: 'Department summary' },
      { key: 'view-employee', label: 'View employee' },
      { key: 'view-team', label: 'View team' },
      { key: 'assignments', label: 'Manager assignments' },
    ] },
    { title: 'Requests', items: [{ key: 'requests', label: 'All requests' }] },
    { items: [
      { key: 'stuck', label: 'Stuck requests', count: stuckRequests.length },
      { key: 'emails', label: 'Email log' },
      { key: 'checks', label: 'Data checks' },
    ] },
  ];

  return (
    <>
      <TopBar onMenu={() => setMenuOpen(true)} />
      <Box sx={{ display: 'flex', minHeight: 'calc(100vh - 56px)' }}>
        <AdminNav
          groups={groups}
          current={tab}
          onSelect={setTab}
          mobileOpen={menuOpen}
          onCloseMobile={() => setMenuOpen(false)}
        />
        <Box
          component="main"
          sx={{ flex: 1, minWidth: 0, px: { xs: 2, sm: 3, lg: 4 }, py: { xs: 2, sm: 3 }, display: 'grid', gap: 2.5, alignContent: 'start', gridTemplateColumns: 'minmax(0, 1fr)' }}
        >
          {loading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
              <CircularProgress />
            </Box>
          ) : (
            <>
              {!processingEnabled && (
                <Alert severity="info">
                  System is in reporting-only mode. Approve/reject actions are disabled.
                </Alert>
              )}

              {/* Every screen but home is headed by its name. */}
              {tab !== 'home' && <Typography variant="h5">{SCREEN_TITLES[tab]}</Typography>}

              {tab === 'home' && (
                <AdminHome
                  pending={pending}
                  heldCount={heldCount}
                  stuckCount={stuckRequests.length}
                  setupCount={setupCount}
                  onGo={setTab}
                />
              )}

              {REQUEST_VIEWS[tab] && (
                <RequestsScreen
                  view={REQUEST_VIEWS[tab]}
                  onView={(v) => setTab(VIEW_SCREEN[v])}
                  pending={pending}
                  stuck={stuckRequests}
                  requests={requests}
                  held={heldRows}
                  heldIncludeClosed={heldIncludeClosed}
                  onHeldIncludeClosed={handleHeldIncludeClosed}
                  stats={stats}
                  processingEnabled={processingEnabled}
                  actionLoading={actionLoading}
                  onApprove={handleApprove}
                  onReject={handleReject}
                  onRemind={handleSendReminder}
                  onRefund={handleRefund}
                  onEdit={(item) => setEditItem(item)}
                  onReprocess={handleReprocess}
                  onAddHeld={handleAddFromHeld}
                  onRetryHeld={(id) => heldAction(id, () => releaseHeldRequest(id), 'Sent on.')}
                  onCancelHeld={(id) => heldAction(id, () => cancelHeldRequest(id), 'Cancelled.')}
                />
              )}

              {tab === 'balances' && (
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

              {tab === 'departments' && stats?.department_summary && (
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

              {tab === 'view-employee' && (
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

              {tab === 'view-team' && (
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

              {tab === 'assignments' && <ManagerAssignments />}

              {tab === 'setup' && (
                <Paper sx={{ p: 3 }}>
                  <EmployeeValidation
                    employees={employees}
                    setupList={employeeSetup}
                    setupLoading={setupRefreshing}
                    onRefreshSetup={refreshEmployeeSetup}
                  />
                </Paper>
              )}

              {tab === 'add' && (
                <AddEmployee
                  processingEnabled={processingEnabled}
                  submitEmployee={createEmployeeAdmin}
                  managerOptions={spUsers}
                  prefill={addPrefill}
                  onCreated={(name) =>
                    setSnack({ open: true, message: `${name} created.`, severity: 'success' })
                  }
                />
              )}

              {tab === 'emails' && (
                <Paper sx={{ p: 3 }}>
                  <EmailLog employees={employees} />
                </Paper>
              )}

              {tab === 'checks' && <RequestColumnsCard processingEnabled={processingEnabled} />}

            </>
          )}
        </Box>
      </Box>

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
    </>
  );
}
