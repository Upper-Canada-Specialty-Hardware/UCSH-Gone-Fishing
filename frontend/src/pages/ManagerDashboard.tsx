import { ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { readLaunchCache, writeLaunchCache } from '../api/launchCache';
import { staffCacheKey } from '../api/intake';
import {
  Alert, Box, Button, Paper, Snackbar, Stack, Tab, Tabs, Typography,
} from '@mui/material';
import { AnimatePresence, m } from 'motion/react';
import { alpha, useTheme } from '@mui/material/styles';
import TeamCalendar from '../components/TeamCalendar';
import { Kpi, Panel } from '../components/Panels';
import RequestsHistoryTable, { HistoryFocus } from '../components/RequestsHistoryTable';
import TeamTab, { RequestedTeamFilter } from '../components/manager/TeamTab';
import AddEmployee, { type AddPrefill } from '../components/AddEmployee';
import NewHireConfirmDialog from '../components/manager/NewHireConfirmDialog';
import DecisionCard from '../components/manager/DecisionCard';
import RejectDialog from '../components/RejectDialog';
import LogSickDayDialog, { SickDayResult } from '../components/manager/LogSickDayDialog';
import {
  AutoHeight, CountUp, DrawnTick, EXIT, Enter, KF, SPRING, Seen, Swap, leaveDelay, staggerChildren, useDirection,
} from '../components/Motion';
import { TeamSkeleton } from '../components/Skeletons';
import PillToggle from '../components/PillToggle';
import { AbsenceDetail, InPerDayChart, LeaveTimeline, MonthlyBars, Sparkline } from '../components/manager/Charts';
import {
  absencesFrom, approveSummary, clashesFor, daysOffByMonth, inPerDay, insights, offThisWeek, totalsByKind, workingDays,
} from '../components/manager/teamStats';
import { kindColor, RequestKind } from '../constants/leaveColors';
import { addDays, dayKey, mondayOf, today } from '../utils/dates';
import { firstName, teamColors, teamLabeler } from '../utils/personColor';
import { askedAgo, daysWaiting, requestWhat, requestWho, shortDate } from '../utils/requestText';
import {
  getMyBalances,
  getTeamMembers,
  getTeamPending,
  getTeamRequests,
  getTeamCalendar,
  getConfig,
  approveRequest,
  rejectRequest,
  createEmployee,
  checkEmployee,
  logSickDay,
  getTeamEmployeeSetup,
  getTeamHeld,
  heldNotOnTeam,
  type HeldWaiting,
} from '../api/client';
import EmployeeValidation from '../components/EmployeeValidation';
import { EmployeeSetupSummary } from '../components/EmployeeSetupList';
import { greeting } from '../utils/greeting';
import { useLiveRequests } from '../hooks/useLiveRequests';
import { LiveEvent } from '../api/events';

/**
 * A new hire's details left by the supervisor's "add this person" email link
 * (see AuthHandler in App.tsx). Read once, then cleared so a refresh does not
 * prefill again.
 *
 * @returns The prefill, or null.
 */
function takeAddEmployeePrefill(): AddPrefill | null {
  const raw = sessionStorage.getItem('add_employee_prefill');
  if (!raw) return null;
  sessionStorage.removeItem('add_employee_prefill');
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/** The tabs, keyed by url segment (#/team/<key>). "add" is reached by its button. */
const TABS = ['today', 'calendar', 'team', 'trends', 'history', 'setup', 'add'] as const;
/** The setup tab's name, also its heading: the admin screen's, for the manager's own team (#463). */
const SETUP_TITLE = 'Potential Employee Setup Issues';
type TabKey = typeof TABS[number];

/** The monthly chart's legend: label and the kind whose colour it uses. */
const MONTH_LEGEND: [string, RequestKind][] = [['Vacation', 'vacation'], ['Sick', 'sick'], ['Other', 'bereavement']];

/**
 * A tile wrapper that turns a read-only KPI into a button opening what it shows
 * (the people on the Team tab, or the Trends tab) (#361).
 *
 * @param props.label - The accessible action label.
 * @param props.onClick - What the tile opens.
 * @param props.children - The KPI tile.
 * @returns The clickable tile.
 */
function TileLink({ label, onClick, children }: { label: string; onClick: () => void; children: ReactNode }) {
  return (
    <Box
      component="button" type="button" aria-label={label} onClick={onClick}
      sx={{
        p: 0, border: 0, background: 'none', font: 'inherit', textAlign: 'left', cursor: 'pointer',
        display: 'grid', borderRadius: 2,
        '& > *': { height: '100%' },                        // the KPI paper fills the button
        '&:hover > *': { borderColor: 'primary.main' },
        '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: 2 },
      }}
    >
      {children}
    </Box>
  );
}

/**
 * The Today tab's held-requests panel (#481), first on the tab: one card per
 * held request-page submission this supervisor still has to act on. Each card
 * says in one bold line what it most likely is (a staff member on the wrong
 * email, or a real new hire) and opens the shared confirm dialog, which offers
 * add/update and Not on my team. Cards wrap and never scroll sideways on a phone.
 *
 * @param props.items - The held new hires waiting on this manager.
 * @param props.processingEnabled - Writes allowed; the decline is disabled otherwise.
 * @param props.busyId - The id of the row whose decline is in flight, or null.
 * @param props.onAdd - Open Add employee prefilled from a held new hire.
 * @param props.onDecline - Hand a held new hire back as "not on my team".
 * @returns The panel.
 */
function NewHiresPanel({ items, processingEnabled, busyId, onAdd, onDecline }: {
  items: HeldWaiting[];
  processingEnabled: boolean;
  busyId: number | null;
  onAdd: (item: HeldWaiting) => void;
  onDecline: (item: HeldWaiting) => void;
}) {
  // The held new hire whose confirm dialog is open, or null when none. The same
  // shared dialog the Team tab uses, so both paths confirm the person the same
  // way before adding or handing the request back (#406).
  const [confirmItem, setConfirmItem] = useState<HeldWaiting | null>(null);
  const theme = useTheme();
  return (
    // Louder than the other panels: an orange edge, at the top of Today.
    <Paper sx={{ p: { xs: 2, sm: 2.5 }, display: 'grid', gap: 1.5, border: 2, borderColor: 'warning.main' }}>
      <Box sx={{ display: 'grid', gap: 0.25 }}>
        <Typography variant="h6" sx={{ fontWeight: 700 }}>
          {items.length === 1 ? 'A request is on hold until you act' : `${items.length} requests are on hold until you act`}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          These came from addresses not on any staff record. Nothing reaches your queue until you deal with them.
        </Typography>
      </Box>
      <Stack spacing={1.5}>
        {items.map((item) => {
          const onStaff = !!item.existing_id;              // the typed name is already on staff
          return (
            <Box key={item.id} sx={{
              border: 1, borderColor: 'divider', borderRadius: 1, p: 1.5, display: 'grid', gap: 0.5,
              bgcolor: alpha(theme.palette.warning.main, 0.05),
            }}>
              <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'baseline' }}>
                <Typography sx={{ fontWeight: 700, wordBreak: 'break-word' }}>{item.name}</Typography>
                <Typography variant="body2" color="text.secondary" sx={{ wordBreak: 'break-word' }}>{item.email}</Typography>
              </Box>
              <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>{item.summary}</Typography>
              {/* What it most likely is, in one bold line. */}
              {onStaff ? (
                <Alert severity="warning" sx={{ py: 0.25 }}>
                  <strong>{item.name} is already on staff.</strong> Most likely they used the wrong email.
                  You do not need to add them: ask them to send it again from their work email.
                </Alert>
              ) : (
                <Alert severity="info" sx={{ py: 0.25 }}>
                  <strong>Not on staff yet.</strong> If they are a new hire on your team, add them and the request comes to you.
                </Alert>
              )}
              {/* How long it has waited, in the same words the queue uses. */}
              <Typography variant="caption" color="text.secondary">
                {item.department ? `${item.department} · ` : ''}{askedAgo({ Created: item.created_at })}
              </Typography>
              <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mt: 0.25 }}>
                {/* One button into the shared confirm dialog, which then offers add
                    and "not on my team" behind the "is it really them?" check. */}
                <Button size="small" variant="contained" onClick={() => setConfirmItem(item)}>
                  {onStaff ? 'Review' : 'Add employee'}
                </Button>
              </Box>
            </Box>
          );
        })}
      </Stack>
      <NewHireConfirmDialog
        item={confirmItem}
        matched={!!confirmItem?.existing_id}
        processingEnabled={processingEnabled}
        busy={busyId !== null && busyId === confirmItem?.id}
        onAdd={onAdd}
        onDecline={onDecline}
        onClose={() => setConfirmItem(null)}
      />
    </Paper>
  );
}

/**
 * The manager dashboard: Today (numbers, the requests waiting on a decision,
 * the next two weeks and a few notes), Calendar (an animated timeline or a
 * month view, and who is in day by day), Team (vacation rings and a card per
 * person), Trends (days off per month, by kind, and time to approve) and
 * History. The open tab is in the url. All of it is drawn from the same five
 * team endpoints the old tabs used; approve and reject call the same routes.
 *
 * @returns The manager dashboard.
 */
/** Everything the manager dashboard loads at launch, as saved for the next launch. */
interface ManagerSnapshot {
  manager_name: string;
  members: any[];
  can_log_sick_days: boolean;
  pending: any[];
  requests: any[];
  calendar: any[];
  processing_enabled: boolean;
}

export default function ManagerDashboard() {
  const theme = useTheme();
  const params = useParams();
  const navigate = useNavigate();
  // The Add employee prefill: from the supervisor's email link at launch, or set
  // by the "Add employee" button in the new-hires panel (#481).
  const [prefill, setPrefill] = useState(takeAddEmployeePrefill);
  const tab: TabKey = TABS.includes(params.tab as TabKey) ? (params.tab as TabKey) : 'today';
  /** Open a tab (changes the url). @param k - Tab key. */
  const go = useCallback((k: TabKey) => navigate(`/team/${k}`), [navigate]);
  // Where History should land when another tab opens it (#361); a new object per click.
  const [historyFocus, setHistoryFocus] = useState<HistoryFocus | null>(null);
  /**
   * Switch to History, opened on one request or filtered (#361).
   *
   * @param target - The request or filter to land on.
   */
  const openHistory = useCallback((target: HistoryFocus) => {
    setHistoryFocus({ ...target });                     // a fresh object so a repeat click re-applies
    go('history');
  }, [go]);

  // Opened from an "add this new hire" email: go straight to Add employee.
  // Runs on arrival only; later tab changes are the manager's own.
  const [arrived] = useState(() => !!prefill && tab !== 'add');
  useEffect(() => {
    if (arrived) navigate('/team/add', { replace: true });
  }, [arrived, navigate]);

  const [calendarView, setCalendarView] = useState<'timeline' | 'month'>('timeline');
  // The leave bar selected on the Calendar timeline, and the day picked on the
  // "who is in" chart (#373); both local to the Calendar tab. The absence is
  // held by key so it always resolves against the live absences list.
  const [selectedAbsenceKey, setSelectedAbsenceKey] = useState<string | null>(null);
  const [pickedDay, setPickedDay] = useState<Date | null>(null);
  // A person the Calendar tab asked the Team tab to open (#373), with a nonce so
  // asking for the same person twice still re-opens them.
  const [teamPersonReq, setTeamPersonReq] = useState<{ id: string; nonce: number } | null>(null);
  // Today's decision queue starts folded to its oldest few; this reveals the rest.
  const [queueExpanded, setQueueExpanded] = useState(false);
  // A filter a Today tile asks the Team tab to apply (with a nonce so clicking
  // the same tile again re-applies it); the Team tab owns its own search (#361).
  const [teamFilterReq, setTeamFilterReq] = useState<RequestedTeamFilter | null>(null);
  const [members, setMembers] = useState<any[]>([]);
  const [pending, setPending] = useState<any[]>([]);
  const [requests, setRequests] = useState<any[]>([]);
  const [calendarEvents, setCalendarEvents] = useState<any[]>([]);
  const [managerName, setManagerName] = useState('');
  const [processingEnabled, setProcessingEnabled] = useState(false);
  // Whether this manager's location is enabled for logging a sick day on an
  // employee's behalf (decided server-side from the Staff Directory, BC for now).
  const [canLogSickDays, setCanLogSickDays] = useState(false);
  const [sickDialogOpen, setSickDialogOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  // Decided cards showing their outcome before they leave: key to true (approved) or false (rejected).
  const [decided, setDecided] = useState<Map<string, boolean>>(new Map());
  const [snack, setSnack] = useState({ open: false, message: '', severity: 'success' as 'success' | 'error' });
  // The request awaiting a reject reason (#433), with an optional follow-up to
  // run once the reject is sent (e.g. closing the calendar's absence card).
  const [rejectTarget, setRejectTarget] = useState<
    { type: string; id: string; name: string; label: string; after?: () => void } | null
  >(null);

  // Live updates: merge each timeline event into the team's requests, and refresh
  // the pending queue when a decision lands from another channel. A decision that
  // moves a balance (approve/reject/refund/cancel) also refetches the team
  // members, so their balance cards and vacation rings follow the change; a
  // timeline event cannot carry a balance. Both are debounced (#328).
  const pendingRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);
  const balancesDirty = useRef(false);                     // a balance-moving event arrived since the last flush
  const onLiveEvent = useCallback((ev: LiveEvent) => {
    const queueTypes = ['approved', 'rejected', 'cancelled', 'submitted', 'sent_to_manager', 'refunded'];
    if (!queueTypes.includes(ev.type)) return;
    if (['approved', 'rejected', 'refunded', 'cancelled'].includes(ev.type)) balancesDirty.current = true;
    if (pendingRefresh.current) clearTimeout(pendingRefresh.current);
    pendingRefresh.current = setTimeout(() => {
      getTeamPending().then((r) => setPending(r.data.pending || [])).catch(() => { /* stale queue until next load */ });
      if (balancesDirty.current) {                          // a balance moved: pull the team cards again
        balancesDirty.current = false;
        getTeamMembers()
          .then((r) => setMembers((r.data.members || []).sort((a: any, b: any) => (a.name || '').localeCompare(b.name || ''))))
          .catch(() => { /* the cards keep their figures until the next load */ });
      }
    }, 300);
  }, []);

  // Potential Employee Setup Issues for the team (#463): the admin screen's
  // list, read from the team route. Like the admin screen it is read on its
  // own: once after launch (so the tab shows its count), when the tab opens,
  // after a fix there, and on a live change while the tab is showing.
  const [employeeSetup, setEmployeeSetup] = useState<EmployeeSetupSummary | null>(null);
  const [setupRefreshing, setSetupRefreshing] = useState(false);
  const setupSeq = useRef(0);                              // numbers each read; only the newest lands
  /**
   * Re-read the team's setup list.
   *
   * @param quiet - True for the read after launch: a failure there only leaves
   *   the tab without a count, so no error message is shown to a manager who
   *   never opened the tab.
   */
  const refreshEmployeeSetup = useCallback(async (quiet = false) => {
    const seq = ++setupSeq.current;                        // this read is now the newest
    setSetupRefreshing(true);
    try {
      const res = await getTeamEmployeeSetup();
      if (seq === setupSeq.current) setEmployeeSetup(res.data);   // an older read landing late is dropped
    } catch (err: any) {
      if (seq !== setupSeq.current || quiet) return;       // a newer read is on its way, or the launch read
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

  // The open tab, for the live handlers below, which are made once.
  const tabRef = useRef(tab);
  tabRef.current = tab;

  // A live change re-reads the list, debounced so a burst of saves costs one
  // read. Off the tab nothing is read: opening it reads afresh.
  const setupTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleSetupRefresh = useCallback(() => {
    if (tabRef.current !== 'setup') return;               // not showing: the next open reads anyway
    if (setupTimer.current) clearTimeout(setupTimer.current);
    setupTimer.current = setTimeout(() => { void refreshEmployeeSetup(); }, 1000);
  }, [refreshEmployeeSetup]);

  // Held new hires waiting on this manager (#481): the Today panel reads these.
  // Refreshed at launch, after an action in the panel, and on a staff change (a
  // new hire added or a held profile saved releases the row off the panel).
  const [heldNewHires, setHeldNewHires] = useState<HeldWaiting[]>([]);
  const [heldActionId, setHeldActionId] = useState<number | null>(null);   // the row being handed back
  const refreshHeld = useCallback(() => {
    getTeamHeld()
      .then((r) => setHeldNewHires(r.data.held || []))
      .catch(() => { /* leave the panel as it was until the next read */ });
  }, []);

  // A staff record changed (a new hire added, an edit, a balance move): re-read
  // the team cards, debounced so a burst of saves costs one read.
  const staffRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onStaffChange = useCallback(() => {
    scheduleSetupRefresh();                                // the setup tab follows staff edits too
    refreshHeld();                                         // a release drops the new hire off the panel (#481)
    if (staffRefresh.current) clearTimeout(staffRefresh.current);
    staffRefresh.current = setTimeout(() => {
      getTeamMembers()
        .then((r) => setMembers((r.data.members || []).sort((a: any, b: any) => (a.name || '').localeCompare(b.name || ''))))
        .catch(() => { /* the cards keep their figures until the next change */ });
    }, 300);
  }, [scheduleSetupRefresh, refreshHeld]);
  useLiveRequests({ scope: 'team', setRows: setRequests, enabled: !loading, onEvent: onLiveEvent, onStaffChange });

  useEffect(() => {
    let cancelled = false;
    const cacheKey = staffCacheKey('team');             // "team:manager:<id>", or null when signed out
    let painted = false;                                // a saved snapshot is already on screen

    /** Put one snapshot of the launch data on screen. */
    const show = (s: ManagerSnapshot) => {
      setManagerName(s.manager_name);
      setMembers(s.members);
      setCanLogSickDays(s.can_log_sick_days);
      setPending(s.pending);
      setRequests(s.requests);
      setCalendarEvents(s.calendar);
      setProcessingEnabled(s.processing_enabled);
      setLoading(false);
    };

    const load = async () => {
      // A relaunch paints the last data at once; the fresh load below replaces it.
      const cached = cacheKey ? await readLaunchCache<ManagerSnapshot>(cacheKey) : null;
      // Only a snapshot of this view's shape is shown; anything else waits for the fresh load.
      const usable = cached && Array.isArray(cached.members) && Array.isArray(cached.pending)
        && Array.isArray(cached.requests) && Array.isArray(cached.calendar);
      if (usable && !cancelled) { show(cached); painted = true; }
      try {
        const [profileRes, membersRes, pendingRes, reqRes, calRes, configRes] = await Promise.all([
          getMyBalances(),
          getTeamMembers(),
          getTeamPending(),
          getTeamRequests(),
          getTeamCalendar(),
          getConfig(),
        ]);
        if (cancelled) return;
        const fresh: ManagerSnapshot = {
          manager_name: profileRes.data.employee?.name || '',
          members: (membersRes.data.members || []).sort((a: any, b: any) => (a.name || '').localeCompare(b.name || '')),
          can_log_sick_days: Boolean(membersRes.data.can_log_sick_days),
          pending: pendingRes.data.pending || [],
          requests: reqRes.data.requests || [],
          calendar: calRes.data.events || [],
          processing_enabled: configRes.data.processing_enabled || false,
        };
        show(fresh);
        if (cacheKey) void writeLaunchCache(cacheKey, fresh);   // the head start for the next launch
      } catch (err: any) {
        if (cancelled) return;
        // With saved data on screen, a failed refresh keeps it rather than blanking the page.
        if (!painted) setError(err.response?.data?.detail || 'Failed to load data');
        setLoading(false);
      }
    };

    void load();
    return () => { cancelled = true; };
  }, []);

  // The setup list is read once the dashboard is up, so the tab shows its count,
  // and again whenever the tab is opened, so edits made elsewhere are reflected.
  useEffect(() => {
    if (!loading) void refreshEmployeeSetup(true);         // quiet: only the tab's count depends on it
  }, [loading, refreshEmployeeSetup]);
  // Held new hires waiting on this manager, read once the dashboard is up (#481).
  useEffect(() => {
    if (!loading) refreshHeld();
  }, [loading, refreshHeld]);
  const prevTab = useRef(tab);
  useEffect(() => {
    if (tab === 'setup' && prevTab.current !== 'setup' && !loading) void refreshEmployeeSetup();
    prevTab.current = tab;
  }, [tab, loading, refreshEmployeeSetup]);

  /**
   * Approve or reject, then drop the request from the waiting list: the card
   * shows its outcome for a moment, then leaves (right when approved, left when
   * rejected) and the cards below slide up into its place.
   *
   * @param type - 'leave', 'overtime' or 'carryover-payout'.
   * @param id - The SharePoint item id.
   * @param yes - True to approve, false to reject.
   * @param reason - The reject reason (ignored when approving); required to reject (#433).
   */
  const decide = useCallback(async (type: string, id: string, yes: boolean, reason = '') => {
    setActionLoading(`${type}-${id}`);
    try {
      await (yes ? approveRequest(type, id) : rejectRequest(type, id, reason));
      const key = `${type}-${id}`;
      setDecided((prev) => new Map(prev).set(key, yes));             // the card shows its outcome
      setTimeout(() => {
        setPending((prev) => prev.filter((p) => !(p.request_type === type && String(p.id) === String(id))));
        setDecided((prev) => { const next = new Map(prev); next.delete(key); return next; });
      }, leaveDelay());                                               // 0 when less motion is asked for
      setSnack({ open: true, message: yes ? 'Request approved' : 'Request rejected', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : yes ? 'Approval failed' : 'Rejection failed', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, []);

  /**
   * After a sick day is approved the employee's sick balance, the request
   * history and the calendar have all changed, so toast the result and pull
   * those three endpoints again.
   *
   * @param result - What the backend returned for the logged sick day.
   */
  const handleSickDayLogged = useCallback(async (result: SickDayResult) => {
    const dayWord = result.days === 1 ? 'day' : 'days';         // match the day count
    setSnack({
      open: true,
      message: `Sick day logged and approved for ${result.employee_name} (${result.days} ${dayWord}).`,
      severity: 'success',
    });
    try {
      // The three views the sick day changed: balances, history and calendar.
      const [membersRes, reqRes, calRes] = await Promise.all([
        getTeamMembers(), getTeamRequests(), getTeamCalendar(),
      ]);
      setMembers((membersRes.data.members || []).sort((a: any, b: any) => (a.name || '').localeCompare(b.name || '')));
      setRequests(reqRes.data.requests || []);
      setCalendarEvents(calRes.data.events || []);
    } catch {
      // The sick day is already approved; a failed refresh only leaves stale
      // numbers on screen until the next reload.
    }
  }, []);

  // Everything the tabs draw, worked out once per data change.
  const names = useMemo(() => members.map((m) => m.name), [members]);
  // One colour per person on this team, in the current light or dark shade.
  const colorOf = useMemo(() => teamColors(names, theme.palette.mode), [names, theme.palette.mode]);
  // One short, unambiguous label per person (first name, or "First L" when a first name repeats).
  const labelOf = useMemo(() => teamLabeler(names), [names]);
  const absences = useMemo(() => absencesFrom(calendarEvents, pending), [calendarEvents, pending]);
  // The selected Calendar bar, resolved from the live absences so a decision that
  // removes it also clears the detail card (#373); its full request row (for the
  // day count); and who is off on the picked day, in team order.
  const selectedAbsence = useMemo(
    () => (selectedAbsenceKey ? absences.find((a) => a.key === selectedAbsenceKey) || null : null),
    [selectedAbsenceKey, absences],
  );
  const selectedAbsenceReq = useMemo(
    () => (selectedAbsence ? requests.find((r) => r.request_type === selectedAbsence.request_type && String(r.id) === selectedAbsence.id) || null : null),
    [selectedAbsence, requests],
  );
  const offOnPickedDay = useMemo(
    () => (pickedDay ? absences.filter((a) => a.start <= pickedDay! && pickedDay! <= a.end) : []),
    [pickedDay, absences],
  );
  const waiting = useMemo(() => [...pending].sort((a, b) => (daysWaiting(b) ?? 0) - (daysWaiting(a) ?? 0)), [pending]);
  const approve = useMemo(() => approveSummary(requests), [requests]);
  const year = today().getFullYear();
  const monthly = useMemo(() => daysOffByMonth(requests, year), [requests, year]);
  // Running total of days off through the year, for the tile's sparkline.
  const cumulative = useMemo(() => {
    let s = 0;
    return monthly.slice(0, today().getMonth() + 1).map((m) => (s += m.vacation + m.sick + m.other));
  }, [monthly]);
  const notes = useMemo(() => insights(members, absences, labelOf), [members, absences, labelOf]);
  const calFrom = mondayOf(today());                     // the calendar shows four weeks from this Monday
  const dir = useDirection(TABS.indexOf(tab));          // which way a tab change slides

  /**
   * Open Add employee prefilled from a held new hire, so AddEmployee's
   * held-for-add flow runs (including the existing-name profile edit) (#481).
   *
   * @param item - The held new hire to add.
   */
  const addHeldNewHire = useCallback((item: HeldWaiting) => {
    // The same prefill shape the supervisor's email link gives, so the add tab
    // asks what happened to the held request and offers the profile edit (#444).
    setPrefill({ title: item.name, email_address: item.email, department: item.department, held_id: item.id });
    go('add');
  }, [go]);

  /**
   * Hand a held new hire back as "not on my team", then refresh the panel (#481).
   *
   * @param item - The held new hire to hand back.
   */
  const declineHeldNewHire = useCallback(async (item: HeldWaiting) => {
    setHeldActionId(item.id);
    try {
      await heldNotOnTeam(item.id);
      setHeldNewHires((prev) => prev.filter((h) => h.id !== item.id));   // drop it from the panel at once
      setSnack({
        open: true,
        message: `${item.name} was handed back; they will be asked to pick another supervisor.`,
        severity: 'success',
      });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'Could not hand that request back', severity: 'error' });
    } finally {
      setHeldActionId(null);
      refreshHeld();                                       // reconcile with the server either way
    }
  }, [refreshHeld]);

  /** Pull the team again after a manager change (balances, add, remove) so the
   *  cards and the waiting count follow it at once (#362, #363). */
  const refreshTeam = useCallback(() => {
    getTeamMembers()
      .then((r) => setMembers((r.data.members || []).sort((a: any, b: any) => (a.name || '').localeCompare(b.name || ''))))
      .catch(() => { /* the cards keep their figures until the next change */ });
    getTeamPending().then((r) => setPending(r.data.pending || [])).catch(() => { /* stale queue until next load */ });
  }, []);

  /** Open the Team tab with a tile's filter applied. @param key - The filter. */
  const openTeamFiltered = useCallback((key: RequestedTeamFilter['key']) => {
    setTeamFilterReq({ key, nonce: Date.now() });         // nonce so the same tile re-applies
    go('team');
  }, [go]);

  /** Open a person's profile on the Team tab from the Calendar (#373).
   *  @param name - The person's display name (from the selected absence). */
  const openTeamPerson = useCallback((name: string) => {
    const m = members.find((x) => x.name === name);       // their row carries the id the profile needs
    if (!m) return;                                        // not on the team (archived or gone): do nothing
    setTeamPersonReq({ id: String(m.id), nonce: Date.now() });  // nonce so the same person re-opens
    go('team');
  }, [members, go]);

  /** Open a teammate's profile on the Team tab from the setup list (#463).
   *  @param id - Their staff id. */
  const openSetupProfile = useCallback((id: string) => {
    setTeamPersonReq({ id: String(id), nonce: Date.now() });  // nonce so the same person re-opens
    go('team');
  }, [go]);
  const setupCount = employeeSetup?.flagged.length ?? 0;   // people with a problem, for the tab label
  // The check's picker: the team without the manager, whose own record is refused by the route.
  const setupPeople = useMemo(() => members.filter((p) => p.name !== managerName), [members, managerName]);

  if (loading) return <TeamSkeleton tab={tab} />;
  if (error) {
    return <Alert severity="error" sx={{ m: 2 }}>{error}</Alert>;
  }

  const oldest = waiting.length ? daysWaiting(waiting[0]) ?? 0 : 0;
  const daysOffTotal = cumulative.length ? cumulative[cumulative.length - 1] : 0;

  return (
    <Box sx={{ display: 'grid', gap: 2.5, animation: `${KF.fade} .24s ease-out both` }}>
      {/* Greeting, what is waiting, the team's size, and the new-hire shortcut. */}
      <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flex: 1, minWidth: 240 }}>
          <Typography variant="h4">{greeting()}{managerName ? `, ${firstName(managerName)}` : ''}</Typography>
          {/* The count follows each decision; at zero the line fades to "all caught up". */}
          <Typography color="text.secondary">
            <Swap k={waiting.length ? 'some' : 'none'} y={4}>
              {waiting.length
                ? <span><CountUp value={waiting.length} duration={0.3} /> request{waiting.length > 1 ? 's need' : ' needs'} your decision.</span>
                : 'You are all caught up.'}
            </Swap>
            {' '}{members.length} {members.length === 1 ? 'person' : 'people'} on your team.
          </Typography>
        </Box>
        <Button variant="outlined" onClick={() => go('add')}>Add a new hire</Button>
      </Box>

      {!processingEnabled && (
        <Alert severity="info">System is in reporting-only mode. Approve/reject actions are disabled.</Alert>
      )}

      <Tabs value={tab === 'add' ? false : tab} onChange={(_, v) => go(v)} variant="scrollable" allowScrollButtonsMobile sx={{ borderBottom: 1, borderColor: 'divider' }}>
        <Tab value="today" label={waiting.length ? <span>Today (<Swap k={waiting.length}>{waiting.length}</Swap>)</span> : 'Today'} />
        <Tab value="calendar" label="Calendar" />
        <Tab value="team" label="Team" />
        <Tab value="trends" label="Trends" />
        <Tab value="history" label="History" />
        <Tab value="setup" label={setupCount ? <span>{SETUP_TITLE} (<Swap k={setupCount}>{setupCount}</Swap>)</span> : SETUP_TITLE} />
      </Tabs>

      {/* The tab's body slides in from the side of the tab picked. */}
      <Enter key={tab} dir={dir} sx={{ display: 'grid', gap: 2.5, minWidth: 0 }}>
      {tab === 'today' && (
        <>
          {/* Requests on hold for this manager (#481) come first: nothing else on
              Today moves until they act, and most are staff on the wrong email. */}
          {heldNewHires.length > 0 && (
            <NewHiresPanel
              items={heldNewHires}
              processingEnabled={processingEnabled}
              busyId={heldActionId}
              onAdd={addHeldNewHire}
              onDecline={declineHeldNewHire}
            />
          )}
          {/* Each tile opens what it counts: the people on the Team tab, or the Trends tab. */}
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(min(150px, 100%), 1fr))', ...staggerChildren({ step: 40 }) }}>
            <TileLink label="Open the people waiting on you" onClick={() => openTeamFiltered('waiting')}>
              <Kpi label="Waiting on you" value={String(waiting.length)} note={waiting.length ? `oldest ${oldest} day${oldest === 1 ? '' : 's'}` : 'nothing waiting'} warn={oldest > 7} />
            </TileLink>
            <TileLink label="Open who is off this week" onClick={() => openTeamFiltered('off')}>
              <Kpi label="Off this week" value={String(offThisWeek(names, absences))} note={`of ${members.length} on the team`} />
            </TileLink>
            <TileLink label="Open Trends" onClick={() => go('trends')}>
              <Kpi
                label="Average time to approve"
                value={approve.avg === null ? '-' : `${approve.avg.toFixed(1)} d`}
                note={approve.avg === null ? 'no approvals in 90 days' : 'last 90 days'}
                spark={approve.months.map((m) => m.avg)}
              />
            </TileLink>
            <TileLink label="Open Trends" onClick={() => go('trends')}>
              <Kpi label="Days off taken this year" value={String(Math.round(daysOffTotal * 10) / 10)} note="approved leave, team total" spark={cumulative} />
            </TileLink>
          </Box>

          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: 'minmax(0, 1fr)', lg: 'minmax(0, 1.1fr) minmax(0, 1fr)' }, alignItems: 'start' }}>
            {/* The queue: a decided card leaves (right when approved, left when rejected) and the
                cards below slide up into its place; extra cards arrive one by one. */}
            <Box sx={{ display: 'grid', gap: 2, position: 'relative' }}>
              <AnimatePresence mode="popLayout" initial={false}>
                {/* Oldest first; only the first five until the manager asks for the rest. */}
                {(queueExpanded ? waiting : waiting.slice(0, 5)).map((item, i) => {
                  const key = `${item.request_type}-${item.id}`;
                  const outcome = decided.get(key);
                  return (
                    <m.div
                      key={key}
                      layout
                      initial={{ opacity: 0, y: 10 }}
                      animate={{ opacity: 1, y: 0, transition: { ...SPRING, delay: Math.max(0, i - 4) * 0.07 } }}
                      exit={outcome === undefined
                        ? { opacity: 0, transition: EXIT }
                        : { opacity: 0, x: outcome ? 60 : -60, rotate: outcome ? 1.5 : -1.5, transition: { duration: 0.3, ease: [0.4, 0, 1, 1] } }}
                    >
                      <DecisionCard
                        item={item}
                        index={i}
                        decided={outcome}
                        clashes={clashesFor(item, absences, labelOf)}
                        processingEnabled={processingEnabled}
                        busy={actionLoading === key}
                        onApprove={() => decide(item.request_type, String(item.id), true)}
                        onReject={() => setRejectTarget({ type: item.request_type, id: String(item.id), name: requestWho(item), label: requestWhat(item) })}
                        colorOf={colorOf}
                      />
                    </m.div>
                  );
                })}
                {/* Reveal or re-fold everything past the first five. */}
                {waiting.length > 5 && (
                  <m.div key="more" layout style={{ justifySelf: 'start' }}>
                    <Button variant="text" onClick={() => setQueueExpanded((v) => !v)} sx={{ textTransform: 'none' }}>
                      {queueExpanded ? 'Show fewer' : `Show ${waiting.length - 5} more`}
                    </Button>
                  </m.div>
                )}
                {waiting.length === 0 && (
                  <m.div key="caught-up" initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }}>
                    <Paper sx={{ p: 4, display: 'grid', justifyItems: 'center', gap: 0.5, textAlign: 'center' }}>
                      <Box sx={{ width: 44, height: 44, borderRadius: 999, display: 'grid', placeItems: 'center', bgcolor: alpha(theme.palette.success.main, 0.14), color: 'success.main' }}>
                        <DrawnTick size={26} delay={0.2} />
                      </Box>
                      <Typography sx={{ fontWeight: 700 }}>You are all caught up</Typography>
                      <Typography variant="body2" color="text.secondary">New requests from your team show up here.</Typography>
                    </Paper>
                  </m.div>
                )}
              </AnimatePresence>
            </Box>
            <Box sx={{ display: 'grid', gap: 2 }}>
              <Panel title="Next two weeks" aside="dashed = asked">
                {/* Only people off in the window, at most eight rows; the rest link to the Calendar tab. */}
                <LeaveTimeline
                  names={names} absences={absences} from={today()} days={14}
                  colorOf={colorOf} labelOf={labelOf} maxRows={8} onShowMore={() => go('calendar')}
                />
              </Panel>
              {notes.length > 0 && (
                <Panel title="Worth knowing">
                  <Box component="ul" sx={{ m: 0, p: 0, listStyle: 'none', display: 'grid', gap: 1.25, ...staggerChildren({ delay: 200, step: 60 }) }}>
                    {notes.map((n) => (
                      <Box component="li" key={n.text} sx={{ display: 'flex', gap: 1.25, alignItems: 'flex-start' }}>
                        <Box
                          sx={{
                            width: 22, height: 22, borderRadius: 999, flex: 'none', display: 'grid', placeItems: 'center', fontSize: 12, fontWeight: 800,
                            color: n.tone === 'warn' ? 'warning.main' : 'info.main',
                            bgcolor: alpha(n.tone === 'warn' ? theme.palette.warning.main : theme.palette.info.main, 0.14),
                          }}
                        >
                          {n.tone === 'warn' ? '!' : 'i'}
                        </Box>
                        <Typography variant="body2">{n.text}</Typography>
                      </Box>
                    ))}
                  </Box>
                </Panel>
              )}
            </Box>
          </Box>
        </>
      )}

      {tab === 'calendar' && (
        <>
          <Paper sx={{ overflow: 'hidden' }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, p: 1.5, borderBottom: 1, borderColor: 'divider', flexWrap: 'wrap' }}>
              <Typography sx={{ fontWeight: 700 }}>
                {calendarView === 'timeline' ? `${shortDate(dayKey(calFrom))} to ${shortDate(dayKey(addDays(calFrom, 27)))}` : 'Approved leave'}
              </Typography>
              <Box sx={{ ml: 'auto' }}>
                <PillToggle
                  name="calendar-view" label="Calendar view" value={calendarView} onChange={setCalendarView}
                  options={[{ key: 'timeline', label: 'Timeline' }, { key: 'month', label: 'Month' }]}
                />
              </Box>
            </Box>
            {/* Switching views cross-fades, and the card's height follows. */}
            <AutoHeight>
              <AnimatePresence mode="wait" initial={false}>
                <m.div key={calendarView} initial={{ opacity: 0 }} animate={{ opacity: 1, transition: { duration: 0.22 } }} exit={{ opacity: 0, transition: EXIT }}>
                  <Box sx={{ p: 2 }}>
                    {calendarView === 'timeline'
                      ? (
                        // Clicking a bar selects it (toggles off on a second click);
                        // its detail card shows below (#373).
                        <LeaveTimeline
                          names={names} absences={absences} from={calFrom} days={28} colorOf={colorOf} labelOf={labelOf} showToggle
                          selectedKey={selectedAbsenceKey}
                          onSelectAbsence={(a) => setSelectedAbsenceKey((cur) => (cur === a.key ? null : a.key))}
                        />
                      )
                      : (
                        // Clicking a block in the month grid selects that request and
                        // jumps to the timeline with its card open (#373).
                        <TeamCalendar
                          events={calendarEvents}
                          onSelectEvent={(ev) => { setSelectedAbsenceKey(`a-${ev.id}`); setCalendarView('timeline'); }}
                        />
                      )}
                  </Box>
                </m.div>
              </AnimatePresence>
            </AutoHeight>
            {calendarView === 'timeline' && (
              // Each row already shows its own dot and name, so the only key left is the dashed = asked one.
              <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap', px: 2, pb: 2, fontSize: 13, color: 'text.secondary' }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                  <Box sx={{ width: 10, height: 10, borderRadius: 0.5, border: '1.5px dashed', borderColor: 'text.secondary' }} />Asked, not approved yet
                </Box>
              </Box>
            )}
            {/* The selected leave bar's detail card, under the timeline (#373). */}
            {calendarView === 'timeline' && selectedAbsence && (
              <Box sx={{ px: 2, pb: 2 }}>
                <AbsenceDetail
                  absence={selectedAbsence}
                  days={selectedAbsenceReq ? Number(selectedAbsenceReq.Days || 0) : null}
                  colorOf={colorOf}
                  processingEnabled={processingEnabled}
                  busy={actionLoading === `${selectedAbsence.request_type}-${selectedAbsence.id}`}
                  openPersonLabel={`Open ${firstName(selectedAbsence.who)}`}
                  onApprove={async () => { await decide(selectedAbsence.request_type, selectedAbsence.id, true); setSelectedAbsenceKey(null); }}
                  onReject={() => setRejectTarget({ type: selectedAbsence.request_type, id: selectedAbsence.id, name: selectedAbsence.who, label: selectedAbsence.label || '', after: () => setSelectedAbsenceKey(null) })}
                  onOpenHistory={() => openHistory({ request_type: selectedAbsence.request_type, id: selectedAbsence.id })}
                  onOpenPerson={() => openTeamPerson(selectedAbsence.who)}
                  onClose={() => setSelectedAbsenceKey(null)}
                />
              </Box>
            )}
          </Paper>
          <Panel title="Who is in, day by day" aside="if every request is approved; weekdays only, holidays not marked">
            <Seen>
              <InPerDayChart
                points={inPerDay(names, absences, workingDays(calFrom, 28))} team={names.length}
                pickedDay={pickedDay}
                onPickDay={(d) => setPickedDay((cur) => (cur && dayKey(cur) === dayKey(d) ? null : d))}
              />
            </Seen>
            {/* The picked day's roster: who is off (kind, and "(asked)" when pending),
                or that everyone is in (#373). A second click on the day hides it. */}
            {pickedDay && (
              <Box sx={{ mt: 1.5, pt: 1.5, borderTop: 1, borderColor: 'divider', display: 'grid', gap: 0.75 }}>
                <Typography sx={{ fontWeight: 700 }}>{shortDate(dayKey(pickedDay))}</Typography>
                {offOnPickedDay.length === 0 ? (
                  <Typography variant="body2" color="text.secondary">Everyone is in.</Typography>
                ) : (
                  offOnPickedDay.map((a) => (
                    <Box key={a.key} sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                      <Box sx={{ width: 8, height: 8, borderRadius: 999, bgcolor: colorOf(a.who), flex: 'none' }} />
                      <Typography variant="body2" sx={{ fontWeight: 600 }}>{labelOf(a.who)}</Typography>
                      <Typography variant="body2" color="text.secondary">{a.label}{a.pending ? ' (asked)' : ''}</Typography>
                    </Box>
                  ))
                )}
              </Box>
            )}
          </Panel>
        </>
      )}

      {tab === 'team' && (
        // The whole Team tab lives in its own component (#361): tiles that filter,
        // Cards or Table, and a person panel that changes balances (#362) and adds
        // or removes people (#363). It is drawn from the team data already loaded.
        <TeamTab
          members={members}
          requests={requests}
          pending={pending}
          absences={absences}
          colorOf={colorOf}
          labelOf={labelOf}
          managerName={managerName}
          processingEnabled={processingEnabled}
          canLogSickDays={canLogSickDays}
          requestedFilter={teamFilterReq}
          onFilterApplied={() => setTeamFilterReq(null)}
          requestedPerson={teamPersonReq}
          onPersonRequested={() => setTeamPersonReq(null)}
          onAddNewHire={() => go('add')}
          onLogSickDay={() => setSickDialogOpen(true)}
          onRefresh={refreshTeam}
          onNotify={(message, severity) => setSnack({ open: true, message, severity })}
          heldNewHires={heldNewHires}
          heldBusyId={heldActionId}
          onAddHeldNewHire={addHeldNewHire}
          onDeclineHeldNewHire={declineHeldNewHire}
        />
      )}

      {tab === 'trends' && (
        <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: '1fr 1fr' } }}>
          <Box sx={{ gridColumn: '1 / -1' }}>
            <Panel title={`Days off per month, ${year}`} aside="approved leave, by start date">
              <Box sx={{ display: 'flex', gap: 2, mb: 1, fontSize: 13, color: 'text.secondary' }}>
                {MONTH_LEGEND.map(([l, k]) => (
                  <Box key={l} sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                    <Box sx={{ width: 10, height: 10, borderRadius: 0.5, bgcolor: kindColor(k, theme.palette.mode) }} />{l}
                  </Box>
                ))}
              </Box>
              {/* Clicking a month opens History filtered to that month's approved leave (#373). */}
              <Seen><MonthlyBars months={monthly} upTo={today().getMonth()} onPickMonth={(k) => openHistory({ status: 'Approved', month: k, year })} /></Seen>
            </Panel>
          </Box>
          <Panel title="By kind" aside={`${year}, approved`}>
            <Seen sx={{ display: 'grid', gap: 1.5 }}>
              {(() => {
                const rows = totalsByKind(requests, year);
                const top = Math.max(1, ...rows.map((r) => r[1]));   // the longest bar fills the track
                // Each kind's row opens the team's requests in History (#361).
                return rows.map(([label, v, k], i) => (
                  <Box
                    key={label} component="button" type="button" onClick={() => openHistory({ kind_label: label, status: 'Approved' })}
                    aria-label={`Open ${label} requests in History`}
                    sx={{
                      display: 'grid', gridTemplateColumns: 'minmax(0, 150px) minmax(0, 1fr) 48px', gap: 1.5, alignItems: 'center',
                      width: '100%', border: 0, background: 'none', font: 'inherit', textAlign: 'left', cursor: 'pointer', px: 0, py: 0.5,
                      borderRadius: 1, '&:hover': { bgcolor: theme.tokens.hover },
                    }}
                  >
                    <Typography variant="body2" color="text.secondary">{label}</Typography>
                    <Box sx={{ height: 10, borderRadius: 999, bgcolor: theme.tokens.hover, overflow: 'hidden' }}>
                      <Box
                        sx={{
                          height: '100%', width: `${(v / top) * 100}%`, bgcolor: kindColor(k, theme.palette.mode), borderRadius: 999,
                          transformOrigin: 'left', animation: `${KF.growX} .7s ease-out ${0.15 + i * 0.07}s both`,
                        }}
                      />
                    </Box>
                    <Typography variant="body2" sx={{ fontWeight: 700, textAlign: 'right' }}>{Math.round(v * 10) / 10}</Typography>
                  </Box>
                ));
              })()}
            </Seen>
          </Panel>
          <Panel title="Time to approve">
            <Box sx={{ display: 'grid', gap: 0.75 }}>
              <Typography sx={{ fontFamily: theme.tokens.display, fontSize: 34, fontWeight: 600 }}>
                {approve.avg === null ? 'No approvals yet' : `${approve.avg.toFixed(1)} days`}
              </Typography>
              <Seen><Sparkline values={approve.months.map((mo) => mo.avg)} height={60} /></Seen>
              <Typography variant="caption" color="text.secondary">
                Average days from a request arriving to its approval over the last 90 days; the line is each of the last six months.
              </Typography>
            </Box>
          </Panel>
        </Box>
      )}

      {tab === 'history' && (
        // minWidth 0 lets this item shrink below the table's natural width; the
        // table is the admin requests table in "team" mode: a Person column,
        // approve/reject on a row still waiting on this manager, and a
        // team-scoped Excel export. Salaried and hourly split into their own
        // tables (#338). The live stream already loaded here updates a row's
        // status in place after a decision.
        <Box sx={{ minWidth: 0 }}>
          <RequestsHistoryTable requests={requests} mode="team" timelineScope="team" processingEnabled={processingEnabled} focus={historyFocus} />
        </Box>
      )}

      {tab === 'setup' && (
        // The admin setup screen for this manager's team only (#463): the same
        // check and list, read and settled through the team routes, with Open
        // profile landing on the person's panel on the Team tab.
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="h5" sx={{ mb: 1 }}>{SETUP_TITLE}</Typography>
          <EmployeeValidation
            employees={setupPeople}
            setupList={employeeSetup}
            setupLoading={setupRefreshing}
            onRefreshSetup={() => refreshEmployeeSetup()}
            scope="team"
            onOpenProfile={openSetupProfile}
          />
        </Box>
      )}

      {tab === 'add' && (
        <AddEmployee
          processingEnabled={processingEnabled}
          submitEmployee={createEmployee}
          checkEmployee={checkEmployee}
          prefill={prefill}
          heldScope="team"
          onCreated={(name) => {
            setSnack({ open: true, message: `${name} added to your team.`, severity: 'success' });
            refreshHeld();                               // the added new hire drops off the panel (#481)
          }}
          onHeldSaved={(id) => {
            // A profile edit of an existing record saved: drop that row from the
            // panel at once, then reconcile with the server (#499). The live
            // staff-change event fires before the release finishes, so the panel
            // is only right once this runs after the save returns.
            setHeldNewHires((prev) => prev.filter((h) => h.id !== id));
            refreshHeld();
          }}
        />
      )}
      </Enter>

      {/* Mounted only where the manager's location is enabled; it picks from the team. */}
      {canLogSickDays && (
        <LogSickDayDialog
          open={sickDialogOpen}
          members={members}
          onClose={() => setSickDialogOpen(false)}
          submitSickDay={logSickDay}
          onLogged={handleSickDayLogged}
        />
      )}

      {/* Every reject asks for a reason first (#433), then runs the decision. */}
      <RejectDialog
        open={rejectTarget !== null}
        personName={rejectTarget?.name}
        requestLabel={rejectTarget?.label}
        busy={rejectTarget !== null && actionLoading === `${rejectTarget.type}-${rejectTarget.id}`}
        onCancel={() => setRejectTarget(null)}
        onConfirm={async (reason) => {
          const t = rejectTarget;
          setRejectTarget(null);
          if (!t) return;
          await decide(t.type, t.id, false, reason);       // the queue/card animation runs as before
          t.after?.();                                     // e.g. close the calendar's absence card
        }}
      />

      <Snackbar
        open={snack.open}
        autoHideDuration={4000}
        onClose={() => setSnack((s) => ({ ...s, open: false }))}
        message={snack.message}
      />
    </Box>
  );
}
