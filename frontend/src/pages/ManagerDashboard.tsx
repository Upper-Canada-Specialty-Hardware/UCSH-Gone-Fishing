import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert, Box, Button, Paper, Snackbar, Tab, Tabs, TextField, Typography,
} from '@mui/material';
import { AnimatePresence, m } from 'motion/react';
import { alpha, useTheme } from '@mui/material/styles';
import TeamCalendar from '../components/TeamCalendar';
import { Kpi, Panel } from '../components/Panels';
import SplitRequestHistory from '../components/admin/SplitRequestHistory';
import AddEmployee from '../components/AddEmployee';
import KindPill from '../components/KindPill';
import DecisionCard from '../components/manager/DecisionCard';
import LogSickDayDialog, { SickDayResult } from '../components/manager/LogSickDayDialog';
import EventBusyIcon from '@mui/icons-material/EventBusy';
import {
  AutoHeight, CountUp, DrawnTick, EXIT, Enter, KF, SPRING, Seen, Swap, leaveDelay, riseAt, staggerChildren, useDirection,
} from '../components/Motion';
import { TeamSkeleton } from '../components/Skeletons';
import PillToggle from '../components/PillToggle';
import { InPerDayChart, LeaveTimeline, MonthlyBars, Sparkline, VacationRings } from '../components/manager/Charts';
import {
  absencesFrom, approveSummary, clashesFor, daysOffByMonth, inPerDay, insights, offThisWeek, totalsByKind, workingDays,
} from '../components/manager/teamStats';
import { kindColor, RequestKind } from '../constants/leaveColors';
import { addDays, dayKey, formatDayRange, mondayOf, today } from '../utils/dates';
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
import { isHourly } from '../utils/employment';
import { useLiveRequests } from '../hooks/useLiveRequests';
import { LiveEvent } from '../api/events';
import LiveNotice from '../components/LiveNotice';

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
 * Initials for an avatar.
 *
 * @param name - The full name.
 * @returns Up to two letters.
 */
function initials(name: string): string {
  return name.split(' ').map((w) => w[0]).slice(0, 2).join('');
}

/**
 * A full-width banner heading one group of team cards in the balances grid.
 *
 * @param props.label - "Salaried" or "Hourly".
 * @param props.count - How many people are in the group.
 * @returns The banner, spanning every grid column.
 */
function GroupBanner({ label, count }: { label: string; count: number }) {
  return (
    <Box sx={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'baseline', gap: 1, pt: 0.5 }}>
      <Typography sx={{ fontWeight: 700 }}>{label}</Typography>
      <Typography variant="caption" color="text.secondary">{count} {count === 1 ? 'person' : 'people'}</Typography>
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
export default function ManagerDashboard() {
  const theme = useTheme();
  const params = useParams();
  const navigate = useNavigate();
  const [prefill] = useState(takeAddEmployeePrefill);
  const tab: TabKey = TABS.includes(params.tab as TabKey) ? (params.tab as TabKey) : 'today';
  /** Open a tab (changes the url). @param k - Tab key. */
  const go = useCallback((k: TabKey) => navigate(`/team/${k}`), [navigate]);

  // Opened from an "add this new hire" email: go straight to Add employee.
  // Runs on arrival only; later tab changes are the manager's own.
  const [arrived] = useState(() => !!prefill && tab !== 'add');
  useEffect(() => {
    if (arrived) navigate('/team/add', { replace: true });
  }, [arrived, navigate]);

  const [calendarView, setCalendarView] = useState<'timeline' | 'month'>('timeline');
  // Today's decision queue starts folded to its oldest few; this reveals the rest.
  const [queueExpanded, setQueueExpanded] = useState(false);
  // The Team tab's name filter, applied to the rings and the member cards.
  const [teamSearch, setTeamSearch] = useState('');
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
  // the pending queue (debounced) when a decision lands from another channel (#328).
  const pendingRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onLiveEvent = useCallback((ev: LiveEvent) => {
    const queueTypes = ['approved', 'rejected', 'cancelled', 'submitted', 'sent_to_manager', 'refunded'];
    if (!queueTypes.includes(ev.type)) return;
    if (pendingRefresh.current) clearTimeout(pendingRefresh.current);
    pendingRefresh.current = setTimeout(() => {
      getTeamPending().then((r) => setPending(r.data.pending || [])).catch(() => { /* stale queue until next load */ });
    }, 600);
  }, []);
  const { status: liveStatus } = useLiveRequests({ scope: 'team', setRows: setRequests, enabled: !loading, onEvent: onLiveEvent });

  useEffect(() => {
    Promise.all([
      getMyBalances(),
      getTeamMembers(),
      getTeamPending(),
      getTeamRequests(),
      getTeamCalendar(),
      getConfig(),
    ])
      .then(([profileRes, membersRes, pendingRes, reqRes, calRes, configRes]) => {
        setManagerName(profileRes.data.employee?.name || '');
        setMembers((membersRes.data.members || []).sort((a: any, b: any) => (a.name || '').localeCompare(b.name || '')));
        setCanLogSickDays(Boolean(membersRes.data.can_log_sick_days));
        setPending(pendingRes.data.pending || []);
        setRequests(reqRes.data.requests || []);
        setCalendarEvents(calRes.data.events || []);
        setProcessingEnabled(configRes.data.processing_enabled || false);
      })
      .catch((err) => setError(err.response?.data?.detail || 'Failed to load data'))
      .finally(() => setLoading(false));
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
  // The Team tab's members after the name filter, matched on the typed text.
  const q = teamSearch.trim().toLowerCase();
  const teamShown = q ? members.filter((mem) => (mem.name || '').toLowerCase().includes(q)) : members;
  const dir = useDirection(TABS.indexOf(tab));          // which way a tab change slides

  // Team balances split by type (#300, #338): salaried keeps every pot; hourly
  // shows Sick and the vacation-taken counter only. isHourly reads the wire flag.
  const salariedShown = teamShown.filter((mem) => !isHourly(mem.balances));
  const hourlyShown = teamShown.filter((mem) => isHourly(mem.balances));

  /**
   * One team member's balance card, matching each type's rules.
   *
   * @param mem - The member and their balances.
   * @param k - The card's index in its group, for the entrance stagger.
   * @returns The animated card.
   */
  const renderMemberCard = (mem: any, k: number) => {
    const b = mem.balances || {};
    // Their next time away from today on, approved or asked.
    const nextOff = absences
      .filter((a) => a.who === mem.name && a.end >= today())
      .sort((x, y) => x.start.getTime() - y.start.getTime())[0];
    const bc = isHourly(b);                            // hourly: Sick plus a vacation counter
    /** One balance line. @param label - Pot. @param v - Days. @param of - Entitlement. @returns The line. */
    const line = (label: string, v: number, of?: number) => (
      <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
        <Typography variant="body2" color="text.secondary">{label}</Typography>
        <Typography variant="body2" sx={{ fontWeight: 700, color: v < 0 ? 'error.main' : 'text.primary' }}>{v}{of ? ` of ${of}` : ''}</Typography>
      </Box>
    );
    return (
      <m.div
        key={mem.id}
        layout
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.96, transition: EXIT }}
        style={{ display: 'grid' }}
      >
        {/* A card lifts slightly under the pointer so it reads as one object. */}
        <Paper sx={{
          p: 2, display: 'grid', gap: 0.75, alignContent: 'start', ...riseAt(k, { step: 40, max: 8 }),
          transition: 'transform .2s cubic-bezier(.2,.8,.2,1), border-color .2s ease',
          '&:hover': { transform: 'translateY(-2px)', borderColor: theme.tokens.line2 },
        }}>
          <Box sx={{ display: 'flex', gap: 1.25, alignItems: 'center', mb: 0.5 }}>
            <Box sx={{ width: 36, height: 36, borderRadius: 999, bgcolor: colorOf(mem.name), color: theme.palette.mode === 'dark' ? theme.palette.background.default : '#fff', display: 'grid', placeItems: 'center', fontWeight: 700, fontSize: 13, flex: 'none' }}>
              {initials(mem.name)}
            </Box>
            <Box sx={{ minWidth: 0 }}>
              {/* Wrap a long name onto a second line instead of cutting it off. */}
              <Typography sx={{ fontWeight: 700, overflowWrap: 'anywhere', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{mem.name}</Typography>
              <Typography variant="caption" color="text.secondary">{mem.department || 'No department'} · {mem.location || 'No location'}</Typography>
            </Box>
          </Box>
          {bc ? (
            <>
              {/* Vacation is a counter here, not a balance: no "of", with a caption. */}
              <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                <Typography variant="body2" color="text.secondary">Vacation taken</Typography>
                <Typography variant="body2" sx={{ fontWeight: 700 }}>{b.vacation_taken ?? 0}</Typography>
              </Box>
              <Typography variant="caption" color="text.secondary" sx={{ mt: -0.5 }}>This year. Counted, not a balance.</Typography>
              {line('Sick', b.sick_balance ?? 0, b.sick_entitlement)}
            </>
          ) : (
            <>
              {line('Vacation', b.vacation_balance ?? 0, b.vacation_entitlement)}
              {line('Sick', b.sick_balance ?? 0, b.sick_entitlement)}
              {line('Make-Up', b.overtime ?? 0)}
              {line('Carry Over', b.carryover ?? 0)}
            </>
          )}
          <Box sx={{ mt: 0.5, pt: 1, borderTop: 1, borderColor: 'divider' }}>
            {nextOff ? (
              <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                <Typography variant="body2" color="text.secondary">Next off</Typography>
                <KindPill kind={nextOff.kind} label={nextOff.label} />
                <Typography variant="body2">
                  {/* Full day format; formatDayRange shows one date when start == end. */}
                  {formatDayRange(dayKey(nextOff.start), dayKey(nextOff.end))}{nextOff.pending ? ' (asked)' : ''}
                </Typography>
              </Box>
            ) : <Typography variant="body2" color="text.secondary">No time off booked.</Typography>}
          </Box>
        </Paper>
      </m.div>
    );
  };

  if (loading) return <TeamSkeleton tab={tab} />;
  if (error) {
    return <Alert severity="error" sx={{ m: 2 }}>{error}</Alert>;
  }

  const oldest = waiting.length ? daysWaiting(waiting[0]) ?? 0 : 0;
  const daysOffTotal = cumulative.length ? cumulative[cumulative.length - 1] : 0;

  return (
    <Box sx={{ display: 'grid', gap: 2.5, animation: `${KF.fade} .24s ease-out both` }}>
      <LiveNotice status={liveStatus} />
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
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(min(150px, 100%), 1fr))', ...staggerChildren({ step: 40 }) }}>
            <Kpi label="Waiting on you" value={String(waiting.length)} note={waiting.length ? `oldest ${oldest} day${oldest === 1 ? '' : 's'}` : 'nothing waiting'} warn={oldest > 7} />
            <Kpi label="Off this week" value={String(offThisWeek(names, absences))} note={`of ${members.length} on the team`} />
            <Kpi
              label="Average time to approve"
              value={approve.avg === null ? '-' : `${approve.avg.toFixed(1)} d`}
              note={approve.avg === null ? 'no approvals in 90 days' : 'last 90 days'}
              spark={approve.months.map((m) => m.avg)}
            />
            <Kpi label="Days off taken this year" value={String(Math.round(daysOffTotal * 10) / 10)} note="approved leave, team total" spark={cumulative} />
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
        <>
          {/* Name filter on the left; the sick-day action sits with it, the team area
              being where picking a team member to log for belongs. */}
          <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', flexWrap: 'wrap' }}>
            <TextField
              label="Search by name" value={teamSearch} onChange={(e) => setTeamSearch(e.target.value)}
              size="small" placeholder="Start typing a name" sx={{ flex: '1 1 240px', maxWidth: 360 }}
            />
            {/* Shown only where the manager's location is enabled; off in reporting-only mode. */}
            {canLogSickDays && (
              <Button
                variant="contained" startIcon={<EventBusyIcon />} sx={{ ml: { sm: 'auto' } }}
                disabled={!processingEnabled} onClick={() => setSickDialogOpen(true)}
              >
                Log sick day
              </Button>
            )}
          </Box>
          {/* Vacation left is a salaried figure; hourly staff have a counter, not a balance. */}
          {salariedShown.length > 0 && (
            <Panel title="Vacation left"><VacationRings members={salariedShown} colorOf={colorOf} labelOf={labelOf} /></Panel>
          )}
          {/* A search fades out the people who no longer match while the rest slide together.
              With both types present the cards split into a Salaried and an Hourly band. */}
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fill, minmax(min(260px, 100%), 1fr))', position: 'relative' }}>
            <AnimatePresence mode="popLayout" initial={false}>
            {salariedShown.length > 0 && hourlyShown.length > 0
              ? [
                  <GroupBanner key="band-salaried" label="Salaried" count={salariedShown.length} />,
                  ...salariedShown.map((mem, k) => renderMemberCard(mem, k)),
                  <GroupBanner key="band-hourly" label="Hourly" count={hourlyShown.length} />,
                  ...hourlyShown.map((mem, k) => renderMemberCard(mem, k)),
                ]
              : teamShown.map((mem, k) => renderMemberCard(mem, k))}
            <m.div key="add" layout style={{ display: 'grid' }}>
              <Button variant="outlined" onClick={() => go('add')} sx={{ minHeight: 180, borderStyle: 'dashed', borderRadius: `${theme.shape.borderRadius}px` }}>
                + Add a new hire
              </Button>
            </m.div>
            </AnimatePresence>
          </Box>
        </>
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
                return rows.map(([label, v, k], i) => (
                  <Box key={label} sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, 150px) minmax(0, 1fr) 48px', gap: 1.5, alignItems: 'center' }}>
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
        // minWidth 0 lets this grid item shrink below the table's natural width; overflow hidden keeps the page from widening.
        <Paper sx={{ p: 2, minWidth: 0, overflow: 'hidden' }}>
          <SplitRequestHistory requests={requests} showEmployee timelineScope="team" />
        </Paper>
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
