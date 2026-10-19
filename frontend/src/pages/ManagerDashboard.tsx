import { ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { readLaunchCache, writeLaunchCache } from '../api/launchCache';
import { staffCacheKey } from '../api/intake';
import {
  Alert, Box, Button, Paper, Snackbar, Tab, Tabs, Typography,
} from '@mui/material';
import { AnimatePresence, m } from 'motion/react';
import { alpha, useTheme } from '@mui/material/styles';
import TeamCalendar from '../components/TeamCalendar';
import { Kpi, Panel } from '../components/Panels';
import RequestsHistoryTable, { HistoryFocus } from '../components/RequestsHistoryTable';
import TeamTab, { RequestedTeamFilter } from '../components/manager/TeamTab';
import AddEmployee from '../components/AddEmployee';
import DecisionCard from '../components/manager/DecisionCard';
import LogSickDayDialog, { SickDayResult } from '../components/manager/LogSickDayDialog';
import {
  AutoHeight, CountUp, DrawnTick, EXIT, Enter, KF, SPRING, Seen, Swap, leaveDelay, staggerChildren, useDirection,
} from '../components/Motion';
import { TeamSkeleton } from '../components/Skeletons';
import PillToggle from '../components/PillToggle';
import { InPerDayChart, LeaveTimeline, MonthlyBars, Sparkline } from '../components/manager/Charts';
import {
  absencesFrom, approveSummary, clashesFor, daysOffByMonth, inPerDay, insights, offThisWeek, totalsByKind, workingDays,
} from '../components/manager/teamStats';
import { kindColor, RequestKind } from '../constants/leaveColors';
import { addDays, dayKey, mondayOf, today } from '../utils/dates';
import { firstName, teamColors, teamLabeler } from '../utils/personColor';
import { daysWaiting, shortDate } from '../utils/requestText';
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
  logSickDay,
} from '../api/client';
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
function takeAddEmployeePrefill(): { title: string; email_address: string; department?: string; location?: string } | null {
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
const TABS = ['today', 'calendar', 'team', 'trends', 'history', 'add'] as const;
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
  const [prefill] = useState(takeAddEmployeePrefill);
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
  // A staff record changed (a new hire added, an edit, a balance move): re-read
  // the team cards, debounced so a burst of saves costs one read.
  const staffRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onStaffChange = useCallback(() => {
    if (staffRefresh.current) clearTimeout(staffRefresh.current);
    staffRefresh.current = setTimeout(() => {
      getTeamMembers()
        .then((r) => setMembers((r.data.members || []).sort((a: any, b: any) => (a.name || '').localeCompare(b.name || ''))))
        .catch(() => { /* the cards keep their figures until the next change */ });
    }, 300);
  }, []);
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

  /**
   * Approve or reject, then drop the request from the waiting list: the card
   * shows its outcome for a moment, then leaves (right when approved, left when
   * rejected) and the cards below slide up into its place.
   *
   * @param type - 'leave', 'overtime' or 'carryover-payout'.
   * @param id - The SharePoint item id.
   * @param yes - True to approve, false to reject.
   */
  const decide = useCallback(async (type: string, id: string, yes: boolean) => {
    setActionLoading(`${type}-${id}`);
    try {
      await (yes ? approveRequest(type, id) : rejectRequest(type, id));
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
      </Tabs>

      {/* The tab's body slides in from the side of the tab picked. */}
      <Enter key={tab} dir={dir} sx={{ display: 'grid', gap: 2.5, minWidth: 0 }}>
      {tab === 'today' && (
        <>
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
                        onReject={() => decide(item.request_type, String(item.id), false)}
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
                      ? <LeaveTimeline names={names} absences={absences} from={calFrom} days={28} colorOf={colorOf} labelOf={labelOf} showToggle />
                      : <TeamCalendar events={calendarEvents} />}
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
          </Paper>
          <Panel title="Who is in, day by day" aside="if every request is approved; weekdays only, holidays not marked">
            <Seen><InPerDayChart points={inPerDay(names, absences, workingDays(calFrom, 28))} team={names.length} /></Seen>
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
          processingEnabled={processingEnabled}
          canLogSickDays={canLogSickDays}
          requestedFilter={teamFilterReq}
          onFilterApplied={() => setTeamFilterReq(null)}
          onLogSickDay={() => setSickDialogOpen(true)}
          onOpenHistory={(r: any) => openHistory({ request_type: r?.request_type, id: r?.id })}
          onRefresh={refreshTeam}
          onNotify={(message, severity) => setSnack({ open: true, message, severity })}
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
              <Seen><MonthlyBars months={monthly} upTo={today().getMonth()} /></Seen>
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

      {tab === 'add' && (
        <AddEmployee
          processingEnabled={processingEnabled}
          submitEmployee={createEmployee}
          prefill={prefill}
          onCreated={(name) =>
            setSnack({ open: true, message: `${name} added to your team.`, severity: 'success' })
          }
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

      <Snackbar
        open={snack.open}
        autoHideDuration={4000}
        onClose={() => setSnack((s) => ({ ...s, open: false }))}
        message={snack.message}
      />
    </Box>
  );
}
