import { useEffect, useState, useCallback } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Box, Typography, Paper, CircularProgress, Alert, Snackbar } from '@mui/material';
import TopBar from '../components/TopBar';
import AdminNav, { NavGroup } from '../components/admin/AdminNav';
import AdminHome from '../components/admin/AdminHome';
import ManagerAssignments from '../components/ManagerAssignments';
import EmployeeValidation from '../components/EmployeeValidation';
import { EmployeeSetupSummary } from '../components/EmployeeSetupList';
import EditRequestDialog from '../components/EditRequestDialog';
import AddEmployee, { ManagerOption } from '../components/AddEmployee';
import EmailLog from '../components/EmailLog';
import HolidaysManager from '../components/HolidaysManager';
import RequestsScreen from '../components/admin/RequestsScreen';
import StaffScreen from '../components/admin/StaffScreen';
import RequestColumnsCard from '../components/admin/RequestColumnsCard';
import { Enter, leaveDelay } from '../components/Motion';
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
  staff: 'Staff',
  // Reached from Staff, not the sidebar.
  assignments: 'Manager assignments',
  requests: 'All requests',
  holidays: 'Company holidays',
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
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [leaving, setLeaving] = useState<Set<string>>(new Set());   // decided pending rows on their way out
  const [snack, setSnack] = useState({ open: false, message: '', severity: 'success' as 'success' | 'error' });

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

  /**
   * Take a decided request out of the pending list: its row fades first, then
   * it is removed (straight away when less motion is asked for).
   *
   * @param type - 'leave', 'overtime' or 'carryover-payout'.
   * @param id - The SharePoint item id.
   */
  const removeDecided = useCallback((type: string, id: string) => {
    const key = `${type}-${id}`;
    setLeaving((prev) => new Set(prev).add(key));
    setTimeout(() => {
      setPending((prev) => prev.filter((p) => !(p.request_type === type && String(p.id) === String(id))));
      setLeaving((prev) => { const next = new Set(prev); next.delete(key); return next; });
    }, leaveDelay());
  }, []);

  const handleApprove = useCallback(async (type: string, id: string) => {
    setActionLoading(`${type}-${id}`);
    try {
      await adminApproveRequest(type, id);
      removeDecided(type, id);                                      // fades the row, then drops it
      setSnack({ open: true, message: 'Request approved', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'Approval failed', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, [removeDecided]);

  const handleReject = useCallback(async (type: string, id: string) => {
    setActionLoading(`${type}-${id}`);
    try {
      await adminRejectRequest(type, id);
      removeDecided(type, id);                                      // fades the row, then drops it
      setSnack({ open: true, message: 'Request rejected', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'Rejection failed', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, [removeDecided]);

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
      { key: 'staff', label: 'Staff' },
      { key: 'setup', label: 'Employee setup', count: setupCount },
      { key: 'add', label: 'Add employee' },
    ] },
    { title: 'Requests', items: [{ key: 'requests', label: 'All requests' }] },
    // The holidays every working-day count skips, edited here since they moved to Postgres.
    { title: 'Calendar', items: [{ key: 'holidays', label: 'Company holidays' }] },
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
          current={tab === 'assignments' ? 'staff' : tab}
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

              {/* The screen eases in on change; the request views share a key so switching them keeps the table's search. */}
              <Enter key={REQUEST_VIEWS[tab] ? 'requests' : tab} sx={{ display: 'grid', gap: 2.5, gridTemplateColumns: 'minmax(0, 1fr)' }}>

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
                  leaving={leaving}
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

              {tab === 'staff' && (
                <StaffScreen
                  employees={employees}
                  requests={requests}
                  setup={employeeSetup}
                  onOpenDashboard={handleOpenDashboard}
                  onCopyLink={handleCopyEmployeeLink}
                  onSendLink={handleSendDashboardLink}
                  onManageAssignments={() => setTab('assignments')}
                />
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

              {tab === 'holidays' && <HolidaysManager />}
              </Enter>
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
