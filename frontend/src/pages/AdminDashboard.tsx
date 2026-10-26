import { useEffect, useRef, useState, useCallback } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Box, Typography, Paper, Alert, Snackbar, Stack } from '@mui/material';
import TopBar from '../components/TopBar';
import { useLiveRequests } from '../hooks/useLiveRequests';
import { LiveEvent } from '../api/events';
import AdminNav, { NavGroup } from '../components/admin/AdminNav';
import AdminHome from '../components/admin/AdminHome';
import ManagerAssignments from '../components/ManagerAssignments';
import EmployeeValidation from '../components/EmployeeValidation';
import { EmployeeSetupSummary } from '../components/EmployeeSetupList';
import EditRequestDialog from '../components/EditRequestDialog';
import AddEmployee, { ManagerOption } from '../components/AddEmployee';
import EmailLog from '../components/EmailLog';
import TextLog from '../components/TextLog';
import HolidaysManager from '../components/HolidaysManager';
import RequestsScreen from '../components/admin/RequestsScreen';
import ManagersOnVacation from '../components/admin/ManagersOnVacation';
import StaffScreen from '../components/admin/StaffScreen';
import RequestColumnsCard from '../components/admin/RequestColumnsCard';
import RequestStorageCard from '../components/admin/RequestStorageCard';
import AdminPasswordCard from '../components/admin/AdminPasswordCard';
import { Enter, leaveDelay } from '../components/Motion';
import { AdminSkeleton } from '../components/Skeletons';
import { HeldRow, OPEN_HELD, RequestView } from '../components/admin/requestRows';
import { readLaunchCache, writeLaunchCache } from '../api/launchCache';
import {
  getAdminBalances,
  getAdminPending,
  getAdminRequests,
  getAdminStats,
  getAdminStuckRequests,
  getAdminVacationCover,
  getAdminEmployeeSetup,
  getConfig,
  getSpUsers,
  createEmployeeAdmin,
  checkEmployeeAdmin,
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
  VacationCoverRequest,
} from '../api/client';

/** Every admin screen, keyed by its url segment (#/admin/<key>). */
const SCREEN_TITLES: Record<string, string> = {
  home: 'Home',
  pending: 'Pending approvals',
  vacation: 'Managers on vacation',
  held: 'Held for new hires',
  setup: 'Potential Employee Setup Issues',
  add: 'Add employee',
  staff: 'Staff',
  // Reached from Staff, not the sidebar.
  assignments: 'Manager assignments',
  requests: 'All requests',
  holidays: 'Company holidays',
  stuck: 'Stuck requests',
  emails: 'Email log',
  texts: 'Text log',
  checks: 'Data checks',
  password: 'Admin password',
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
/** Where the admin launch data is saved on this device. */
const ADMIN_CACHE_KEY = 'admin';

/** Everything the admin dashboard loads at launch, as saved for the next launch. */
interface AdminSnapshot {
  employees: any[];
  pending: any[];
  requests: any[];
  stuck: any[];
  setup: EmployeeSetupSummary | null;
  stats: any;
  processing_enabled: boolean;
  held: HeldRow[];
  vacation: VacationCoverRequest[];
}

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
  const [vacationRows, setVacationRows] = useState<VacationCoverRequest[]>([]);   // #370 managers-on-vacation
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
    { title: string; email_address: string; department: string; location: string } | null
  >(null);
  const handleAddFromHeld = useCallback((row: HeldRow) => {
    setAddPrefill({ title: row.name, email_address: row.email, department: row.department, location: row.location });
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

    /** Put one snapshot of the launch data on screen. */
    const show = (s: AdminSnapshot) => {
      setEmployees(s.employees);
      setPending(s.pending);
      setRequests(s.requests);
      setStuckRequests(s.stuck);
      setEmployeeSetup(s.setup);
      setStats(s.stats);
      setProcessingEnabled(s.processing_enabled);
      setHeldRows(s.held || []);                                  // an older saved snapshot may lack it
      // A snapshot saved by an older build has no vacation list (#370); empty, not
      // undefined, or the nav count's .length throws and blanks the whole page.
      setVacationRows(s.vacation || []);
      setLoading(false);
    };

    const loadData = async () => {
      // A relaunch paints the last data at once; the fresh load below replaces it.
      const cached = await readLaunchCache<AdminSnapshot>(ADMIN_CACHE_KEY);
      if (cached && !cancelled) show(cached);
      while (!cancelled) {
        try {
          const [balRes, pendRes, reqRes, statsRes, stuckRes, setupRes, configRes, heldRes, vacRes] = await Promise.all([
            getAdminBalances(),
            getAdminPending(),
            getAdminRequests(),
            getAdminStats(),
            getAdminStuckRequests(),
            getAdminEmployeeSetup(),
            getConfig(),
            // A failure here must not hold up the dashboard; the held view just shows empty.
            getHeldRequests(false).catch(() => ({ data: { held: [] } })),
            // Same: a failure leaves the managers-on-vacation section empty, not the dashboard broken.
            getAdminVacationCover().catch(() => ({ data: { requests: [] } })),
          ]);
          if (cancelled) return;
          const fresh: AdminSnapshot = {
            employees: balRes.data.employees || [],
            pending: pendRes.data.pending || [],
            requests: reqRes.data.requests || [],
            stuck: stuckRes.data.stuck || [],
            setup: setupRes.data,
            stats: statsRes.data,
            processing_enabled: configRes.data.processing_enabled || false,
            held: heldRes.data.held || [],
            vacation: vacRes.data.requests || [],
          };
          show(fresh);
          void writeLaunchCache(ADMIN_CACHE_KEY, fresh);   // the head start for the next launch
          return;
        } catch {
          await new Promise((r) => setTimeout(r, 5000));
        }
      }
    };

    loadData();
    return () => { cancelled = true; };
  }, []);

  // Re-fetch the figures the staff screen shows, after a person's record is saved
  // in the profile, so the table row follows the change without a full reload.
  const refreshStaffData = useCallback(async () => {
    try {
      const [balRes, reqRes] = await Promise.all([getAdminBalances(), getAdminRequests()]);
      setEmployees(balRes.data.employees || []);
      setRequests(reqRes.data.requests || []);
    } catch {
      /* the save already succeeded; the table just keeps the pre-save figures */
    }
  }, []);

  // Re-runs the directory-wide setup check on its own. It is the expensive read
  // of the set, so it is not folded into the dashboard's other refreshes: it
  // runs when the setup screen opens, after a fix there, and on a live change
  // while that screen is showing (see scheduleSetupRefresh).
  const setupSeq = useRef(0);                              // numbers each read; only the newest lands
  const refreshEmployeeSetup = useCallback(async () => {
    const seq = ++setupSeq.current;                        // this read is now the newest
    setSetupRefreshing(true);
    try {
      const res = await getAdminEmployeeSetup();
      if (seq === setupSeq.current) setEmployeeSetup(res.data);   // an older read landing late is dropped
    } catch (err: any) {
      if (seq !== setupSeq.current) return;                // a newer read is on its way
      const detail = err.response?.data?.detail;
      setSnack({
        open: true,
        message: typeof detail === 'string' ? detail : 'Could not refresh the setup check',
        severity: 'error',
      });
    } finally {
      if (seq === setupSeq.current) setSetupRefreshing(false);   // the newest read clears the bar
    }
  }, []);

  // The screen showing, for the live handlers below, which are made once.
  const tabRef = useRef(tab);
  tabRef.current = tab;

  // A live change re-reads the setup list, debounced so a burst of saves costs
  // one read. Off the setup screen nothing is read: opening it reads afresh.
  const setupTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleSetupRefresh = useCallback(() => {
    if (tabRef.current !== 'setup') return;               // not showing: the next open reads anyway
    if (setupTimer.current) clearTimeout(setupTimer.current);
    setupTimer.current = setTimeout(() => { void refreshEmployeeSetup(); }, 1000);
  }, [refreshEmployeeSetup]);

  // Opening the setup screen re-reads the list, so edits made elsewhere (a
  // profile, say) are already reflected. The first screen of a visit skips it:
  // the launch load is reading the list already.
  const prevTab = useRef(tab);
  useEffect(() => {
    if (tab === 'setup' && prevTab.current !== 'setup') void refreshEmployeeSetup();
    prevTab.current = tab;
  }, [tab, refreshEmployeeSetup]);

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
      // Also drop it from the managers-on-vacation section at once (#370).
      setVacationRows((prev) => prev.filter((v) => !(v.request_type === type && String(v.id) === String(id))));
      setSnack({ open: true, message: 'Request approved', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'Approval failed', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, [removeDecided]);

  const handleReject = useCallback(async (type: string, id: string, reason: string) => {
    setActionLoading(`${type}-${id}`);
    try {
      await adminRejectRequest(type, id, reason);                   // the reason the child dialog collected (#433)
      removeDecided(type, id);                                      // fades the row, then drops it
      // Also drop it from the managers-on-vacation section at once (#370).
      setVacationRows((prev) => prev.filter((v) => !(v.request_type === type && String(v.id) === String(id))));
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

  // Resolves true once the link is on the clipboard, so the button can confirm in place.
  const handleCopyEmployeeLink = useCallback(async (targetId: string) => {
    try {
      const res = await getEmployeeDashboardLink(targetId);
      await navigator.clipboard.writeText(res.data.url);
      setSnack({ open: true, message: 'Employee dashboard link copied to clipboard', severity: 'success' });
      return true;
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      const message = typeof detail === 'string' ? detail : 'Failed to copy link';
      setSnack({ open: true, message, severity: 'error' });
      return false;
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

  // Refetch the request lists and their totals after a new request or a delete,
  // so the rebuilt requests table shows the real state once an action lands.
  const reloadRequests = useCallback(async () => {
    try {
      const [pendRes, reqRes, stuckRes, statsRes] = await Promise.all([
        getAdminPending(),
        getAdminRequests(),
        getAdminStuckRequests(),
        getAdminStats(),
      ]);
      setPending(pendRes.data.pending || []);
      setRequests(reqRes.data.requests || []);
      setStuckRequests(stuckRes.data.stuck || []);
      setStats(statsRes.data);
    } catch {
      setSnack({ open: true, message: 'Could not refresh the requests', severity: 'error' });
    }
  }, []);

  // Refetch only the derived views after a live event: the pending queue, the
  // stuck list and the totals. The full /admin/requests list is NOT pulled here;
  // the live stream already merges each event into the loaded rows, so re-reading
  // all of it (thousands of rows) on every decision is wasteful (#328).
  const reloadDerived = useCallback(async () => {
    try {
      const [pendRes, stuckRes, statsRes, vacRes] = await Promise.all([
        getAdminPending(),
        getAdminStuckRequests(),
        getAdminStats(),
        // Refresh the managers-on-vacation section too: a decision there, or a
        // manager returning, changes who is still stuck (#370).
        getAdminVacationCover().catch(() => ({ data: { requests: [] } })),
      ]);
      setPending(pendRes.data.pending || []);
      setStuckRequests(stuckRes.data.stuck || []);
      setStats(statsRes.data);
      setVacationRows(vacRes.data.requests || []);
    } catch {
      /* the merged rows stay current; the derived lists refresh on the next event */
    }
  }, []);

  // Live updates: merge each timeline event into the requests table, and refresh
  // the derived views (debounced ~2s) when a decision lands from another channel
  // so every admin view stays current without re-reading the whole list (#328).
  const liveRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);
  const needFullList = useRef(false);                      // a new request arrived since the last flush
  const onLiveEvent = useCallback((ev: LiveEvent) => {
    const refreshTypes = ['approved', 'rejected', 'cancelled', 'submitted', 'sent_to_manager', 'refunded'];
    if (!refreshTypes.includes(ev.type)) return;
    scheduleSetupRefresh();                                // a decision can add or clear a setup problem
    // A new request's merged row holds only the event's fields (no location,
    // email or description), so a submission pulls the full list once; they are
    // rare next to decisions, which only refresh the derived views.
    if (ev.type === 'submitted') needFullList.current = true;
    if (liveRefresh.current) clearTimeout(liveRefresh.current);
    liveRefresh.current = setTimeout(() => {
      const full = needFullList.current;
      needFullList.current = false;
      void (full ? reloadRequests() : reloadDerived());
    }, 500);
  }, [reloadDerived, reloadRequests, scheduleSetupRefresh]);
  // A staff record changed (added, edited, a balance moved): re-read the staff
  // figures, debounced so a burst of saves costs one read.
  const staffRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onStaffChange = useCallback(() => {
    scheduleSetupRefresh();                                // the setup list follows staff edits too
    if (staffRefresh.current) clearTimeout(staffRefresh.current);
    staffRefresh.current = setTimeout(() => {
      getAdminBalances()
        .then((r) => setEmployees(r.data.employees || []))
        .catch(() => { /* the table keeps its figures until the next change */ });
    }, 300);
  }, [scheduleSetupRefresh]);
  useLiveRequests({ scope: 'admin', setRows: setRequests, enabled: !loading, onEvent: onLiveEvent, onStaffChange });

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
      { key: 'vacation', label: 'Managers on vacation', count: vacationRows.length },
      { key: 'held', label: 'Held for new hires', count: heldCount },
    ] },
    { title: 'People', items: [
      { key: 'staff', label: 'Staff' },
      { key: 'setup', label: 'Potential Employee Setup Issues', count: setupCount },
      { key: 'add', label: 'Add employee' },
    ] },
    { title: 'Requests', items: [{ key: 'requests', label: 'All requests' }] },
    // The holidays every working-day count skips, edited here since they moved to Postgres.
    { title: 'Calendar', items: [{ key: 'holidays', label: 'Company holidays' }] },
    { items: [
      { key: 'stuck', label: 'Stuck requests', count: stuckRequests.length },
      { key: 'emails', label: 'Email log' },
      { key: 'texts', label: 'Text log' },
      { key: 'checks', label: 'Data checks' },
      { key: 'password', label: 'Admin password' },
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
            <AdminSkeleton screen={tab} table={!!REQUEST_VIEWS[tab]} title={tab !== 'home' ? SCREEN_TITLES[tab] : undefined} />
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
                  vacationCount={vacationRows.length}
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
                  onReload={reloadRequests}
                />
              )}

              {tab === 'vacation' && (
                <ManagersOnVacation
                  requests={vacationRows}
                  processingEnabled={processingEnabled}
                  actionLoading={actionLoading}
                  onApprove={handleApprove}
                  onReject={handleReject}
                />
              )}

              {tab === 'staff' && (
                <StaffScreen
                  employees={employees}
                  requests={requests}
                  processingEnabled={processingEnabled}
                  onManageAssignments={() => setTab('assignments')}
                  onReload={refreshStaffData}
                  onNotify={(message, severity) => setSnack({ open: true, message, severity })}
                />
              )}

              {tab === 'assignments' && <ManagerAssignments />}

              {tab === 'setup' && (
                // No outer card: the check and the list are their own cards on the page.
                <EmployeeValidation
                  employees={employees}
                  setupList={employeeSetup}
                  setupLoading={setupRefreshing}
                  onRefreshSetup={refreshEmployeeSetup}
                />
              )}

              {tab === 'add' && (
                <AddEmployee
                  processingEnabled={processingEnabled}
                  submitEmployee={createEmployeeAdmin}
                  checkEmployee={checkEmployeeAdmin}
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

              {tab === 'texts' && (
                <Paper sx={{ p: 3 }}>
                  <TextLog employees={employees} />
                </Paper>
              )}

              {tab === 'checks' && (
                <Stack spacing={2}>
                  {/* Where requests live, and the copy before the move to Postgres. */}
                  <RequestStorageCard processingEnabled={processingEnabled} />
                  <RequestColumnsCard processingEnabled={processingEnabled} />
                </Stack>
              )}

              {tab === 'holidays' && <HolidaysManager />}

              {tab === 'password' && <AdminPasswordCard />}
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
