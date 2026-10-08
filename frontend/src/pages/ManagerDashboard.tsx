import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert, Box, Button, Chip, CircularProgress, Paper, Snackbar, Tab, Tabs, Typography,
} from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import TeamCalendar from '../components/TeamCalendar';
import RequestHistory from '../components/RequestHistory';
import AddEmployee from '../components/AddEmployee';
import KindPill from '../components/KindPill';
import DecisionCard from '../components/manager/DecisionCard';
import { InPerDayChart, LeaveTimeline, MonthlyBars, Sparkline, VacationRings } from '../components/manager/Charts';
import {
  absencesFrom, approveSummary, clashesFor, daysOffByMonth, inPerDay, insights, offThisWeek, totalsByKind, workingDays,
} from '../components/manager/teamStats';
import { kindColor, RequestKind } from '../constants/leaveColors';
import { addDays, dayKey, mondayOf, today } from '../utils/dates';
import { firstName, teamColors } from '../utils/personColor';
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
} from '../api/client';

/**
 * A new hire's details left by the supervisor's "add this person" email link
 * (see AuthHandler in App.tsx). Read once, then cleared so a refresh does not
 * prefill again.
 *
 * @returns The prefill, or null.
 */
function takeAddEmployeePrefill(): { title: string; email_address: string; location: string } | null {
  const raw = sessionStorage.getItem('add_employee_prefill');
  if (!raw) return null;
  sessionStorage.removeItem('add_employee_prefill');
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/** The tabs, keyed by url segment (#/manager/<key>). "add" is reached by its button. */
const TABS = ['today', 'calendar', 'team', 'trends', 'history', 'add'] as const;
type TabKey = typeof TABS[number];

/** The monthly chart's legend: label and the kind whose colour it uses. */
const MONTH_LEGEND: [string, RequestKind][] = [['Vacation', 'vacation'], ['Sick', 'sick'], ['Other', 'bereavement']];

/**
 * "Good morning" by the time of day here.
 *
 * @returns The greeting.
 */
function greeting(): string {
  const h = new Date().getHours();
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
}

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
 * One number tile: a label, the number, a line under it, and an optional sparkline.
 *
 * @param props.label - What it counts.
 * @param props.value - The number, already formatted.
 * @param props.note - The line under it.
 * @param props.warn - Show the note in the warn colour.
 * @param props.spark - Sparkline points, oldest first.
 * @returns The tile.
 */
function Kpi({ label, value, note, warn, spark }: { label: string; value: string; note: string; warn?: boolean; spark?: number[] }) {
  const theme = useTheme();
  return (
    <Paper sx={{ p: 2, display: 'grid', alignContent: 'start', gap: 0.25 }}>
      <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
      <Typography sx={{ fontFamily: theme.tokens.display, fontSize: 32, fontWeight: 600, lineHeight: 1.15 }}>{value}</Typography>
      <Typography variant="caption" sx={{ color: warn ? 'warning.main' : 'text.secondary', fontWeight: warn ? 700 : 400 }}>{note}</Typography>
      {spark && <Sparkline values={spark} />}
    </Paper>
  );
}

/**
 * A titled panel.
 *
 * @param props.title - The heading.
 * @param props.aside - Small text at the right of the heading.
 * @returns The panel.
 */
function Panel({ title, aside, children }: { title: string; aside?: string; children: React.ReactNode }) {
  return (
    <Paper sx={{ overflow: 'hidden' }}>
      <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, px: 2.25, pt: 1.75, pb: 1, flexWrap: 'wrap' }}>
        <Typography sx={{ fontWeight: 700 }}>{title}</Typography>
        {aside && <Typography variant="caption" color="text.secondary" sx={{ ml: 'auto' }}>{aside}</Typography>}
      </Box>
      <Box sx={{ px: 2.25, pb: 2.25 }}>{children}</Box>
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
export default function ManagerDashboard() {
  const theme = useTheme();
  const params = useParams();
  const navigate = useNavigate();
  const [prefill] = useState(takeAddEmployeePrefill);
  const tab: TabKey = TABS.includes(params.tab as TabKey) ? (params.tab as TabKey) : 'today';
  /** Open a tab (changes the url). @param k - Tab key. */
  const go = useCallback((k: TabKey) => navigate(`/manager/${k}`), [navigate]);

  // Opened from an "add this new hire" email: go straight to Add employee.
  // Runs on arrival only; later tab changes are the manager's own.
  const [arrived] = useState(() => !!prefill && tab !== 'add');
  useEffect(() => {
    if (arrived) navigate('/manager/add', { replace: true });
  }, [arrived, navigate]);

  const [calendarView, setCalendarView] = useState<'timeline' | 'month'>('timeline');
  const [members, setMembers] = useState<any[]>([]);
  const [pending, setPending] = useState<any[]>([]);
  const [requests, setRequests] = useState<any[]>([]);
  const [calendarEvents, setCalendarEvents] = useState<any[]>([]);
  const [managerName, setManagerName] = useState('');
  const [processingEnabled, setProcessingEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [snack, setSnack] = useState({ open: false, message: '', severity: 'success' as 'success' | 'error' });

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
        setPending(pendingRes.data.pending || []);
        setRequests(reqRes.data.requests || []);
        setCalendarEvents(calRes.data.events || []);
        setProcessingEnabled(configRes.data.processing_enabled || false);
      })
      .catch((err) => setError(err.response?.data?.detail || 'Failed to load data'))
      .finally(() => setLoading(false));
  }, []);

  /**
   * Approve or reject, then drop the request from the waiting list.
   *
   * @param type - 'leave', 'overtime' or 'carryover-payout'.
   * @param id - The SharePoint item id.
   * @param yes - True to approve, false to reject.
   */
  const decide = useCallback(async (type: string, id: string, yes: boolean) => {
    setActionLoading(`${type}-${id}`);
    try {
      await (yes ? approveRequest(type, id) : rejectRequest(type, id));
      setPending((prev) => prev.filter((p) => !(p.request_type === type && String(p.id) === String(id))));
      setSnack({ open: true, message: yes ? 'Request approved' : 'Request rejected', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : yes ? 'Approval failed' : 'Rejection failed', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, []);

  // Everything the tabs draw, worked out once per data change.
  const names = useMemo(() => members.map((m) => m.name), [members]);
  // One colour per person on this team, in the current light or dark shade.
  const colorOf = useMemo(() => teamColors(names, theme.palette.mode), [names, theme.palette.mode]);
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
  const notes = useMemo(() => insights(members, absences), [members, absences]);
  const calFrom = mondayOf(today());                     // the calendar shows four weeks from this Monday

  if (loading) {
    return <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}><CircularProgress /></Box>;
  }
  if (error) {
    return <Alert severity="error" sx={{ m: 2 }}>{error}</Alert>;
  }

  const oldest = waiting.length ? daysWaiting(waiting[0]) ?? 0 : 0;
  const daysOffTotal = cumulative.length ? cumulative[cumulative.length - 1] : 0;

  return (
    <Box sx={{ display: 'grid', gap: 2.5 }}>
      {/* Greeting, what is waiting, the team's size, and the new-hire shortcut. */}
      <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flex: 1, minWidth: 240 }}>
          <Typography variant="h4">{greeting()}{managerName ? `, ${firstName(managerName)}` : ''}</Typography>
          <Typography color="text.secondary">
            {waiting.length ? `${waiting.length} request${waiting.length > 1 ? 's need' : ' needs'} your decision.` : 'You are all caught up.'}
            {' '}{members.length} {members.length === 1 ? 'person' : 'people'} on your team.
          </Typography>
        </Box>
        <Button variant="outlined" onClick={() => go('add')}>Add a new hire</Button>
      </Box>

      {!processingEnabled && (
        <Alert severity="info">System is in reporting-only mode. Approve/reject actions are disabled.</Alert>
      )}

      <Tabs value={tab === 'add' ? false : tab} onChange={(_, v) => go(v)} variant="scrollable" allowScrollButtonsMobile sx={{ borderBottom: 1, borderColor: 'divider' }}>
        <Tab value="today" label={waiting.length ? `Today (${waiting.length})` : 'Today'} />
        <Tab value="calendar" label="Calendar" />
        <Tab value="team" label="Team" />
        <Tab value="trends" label="Trends" />
        <Tab value="history" label="History" />
      </Tabs>

      {tab === 'today' && (
        <>
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(min(150px, 100%), 1fr))' }}>
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
            <Box sx={{ display: 'grid', gap: 2 }}>
              {waiting.length ? waiting.map((item) => (
                <DecisionCard
                  key={`${item.request_type}-${item.id}`}
                  item={item}
                  clashes={clashesFor(item, absences)}
                  processingEnabled={processingEnabled}
                  busy={actionLoading === `${item.request_type}-${item.id}`}
                  onApprove={() => decide(item.request_type, String(item.id), true)}
                  onReject={() => decide(item.request_type, String(item.id), false)}
                  colorOf={colorOf}
                />
              )) : (
                <Paper sx={{ p: 4, display: 'grid', justifyItems: 'center', gap: 0.5, textAlign: 'center' }}>
                  <Box sx={{ width: 44, height: 44, borderRadius: 999, display: 'grid', placeItems: 'center', bgcolor: alpha(theme.palette.success.main, 0.14), color: 'success.main', fontWeight: 800, fontSize: 20 }}>✓</Box>
                  <Typography sx={{ fontWeight: 700 }}>You are all caught up</Typography>
                  <Typography variant="body2" color="text.secondary">New requests from your team show up here.</Typography>
                </Paper>
              )}
            </Box>
            <Box sx={{ display: 'grid', gap: 2 }}>
              <Panel title="Next two weeks" aside="dashed = asked">
                <LeaveTimeline names={names} absences={absences} from={today()} days={14} colorOf={colorOf} />
              </Panel>
              {notes.length > 0 && (
                <Panel title="Worth knowing">
                  <Box component="ul" sx={{ m: 0, p: 0, listStyle: 'none', display: 'grid', gap: 1.25 }}>
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
              <Box sx={{ ml: 'auto', display: 'flex', gap: 1 }}>
                {(['timeline', 'month'] as const).map((v) => (
                  <Chip
                    key={v} label={v === 'timeline' ? 'Timeline' : 'Month'} onClick={() => setCalendarView(v)} aria-pressed={calendarView === v}
                    color={calendarView === v ? 'primary' : 'default'} variant={calendarView === v ? 'filled' : 'outlined'}
                  />
                ))}
              </Box>
            </Box>
            <Box sx={{ p: 2 }}>
              {calendarView === 'timeline'
                ? <LeaveTimeline names={names} absences={absences} from={calFrom} days={28} colorOf={colorOf} />
                : <TeamCalendar events={calendarEvents} />}
            </Box>
            {calendarView === 'timeline' && (
              <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap', px: 2, pb: 2, fontSize: 13, color: 'text.secondary' }}>
                {names.map((n) => (
                  <Box key={n} sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                    <Box sx={{ width: 10, height: 10, borderRadius: 0.5, bgcolor: colorOf(n) }} />{n}
                  </Box>
                ))}
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                  <Box sx={{ width: 10, height: 10, borderRadius: 0.5, border: '1.5px dashed', borderColor: 'text.secondary' }} />Asked, not approved yet
                </Box>
              </Box>
            )}
          </Paper>
          <Panel title="Who is in, day by day" aside="if every request is approved; weekdays only, holidays not marked">
            <InPerDayChart points={inPerDay(names, absences, workingDays(calFrom, 28))} team={names.length} />
          </Panel>
        </>
      )}

      {tab === 'team' && (
        <>
          <Panel title="Vacation left"><VacationRings members={members} colorOf={colorOf} /></Panel>
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fill, minmax(min(260px, 100%), 1fr))' }}>
            {members.map((m) => {
              const b = m.balances || {};
              // Their next time away from today on, approved or asked.
              const nextOff = absences
                .filter((a) => a.who === m.name && a.end >= today())
                .sort((x, y) => x.start.getTime() - y.start.getTime())[0];
              /** One balance line. @param label - Pot. @param v - Days. @param of - Entitlement. @returns The line. */
              const line = (label: string, v: number, of?: number) => (
                <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                  <Typography variant="body2" color="text.secondary">{label}</Typography>
                  <Typography variant="body2" sx={{ fontWeight: 700, color: v < 0 ? 'error.main' : 'text.primary' }}>{v}{of ? ` of ${of}` : ''}</Typography>
                </Box>
              );
              return (
                <Paper key={m.id} sx={{ p: 2, display: 'grid', gap: 0.75, alignContent: 'start' }}>
                  <Box sx={{ display: 'flex', gap: 1.25, alignItems: 'center', mb: 0.5 }}>
                    <Box sx={{ width: 36, height: 36, borderRadius: 999, bgcolor: colorOf(m.name), color: theme.palette.mode === 'dark' ? theme.palette.background.default : '#fff', display: 'grid', placeItems: 'center', fontWeight: 700, fontSize: 13, flex: 'none' }}>
                      {initials(m.name)}
                    </Box>
                    <Box sx={{ minWidth: 0 }}>
                      <Typography sx={{ fontWeight: 700 }} noWrap>{m.name}</Typography>
                      <Typography variant="caption" color="text.secondary">{m.department || 'No department'} · {m.location || 'No location'}</Typography>
                    </Box>
                  </Box>
                  {line('Vacation', b.vacation_balance ?? 0, b.vacation_entitlement)}
                  {line('Sick', b.sick_balance ?? 0, b.sick_entitlement)}
                  {line('Make-Up', b.overtime ?? 0)}
                  {line('Carry Over', b.carryover ?? 0)}
                  <Box sx={{ mt: 0.5, pt: 1, borderTop: 1, borderColor: 'divider' }}>
                    {nextOff ? (
                      <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                        <Typography variant="body2" color="text.secondary">Next off</Typography>
                        <KindPill kind={nextOff.kind} label={nextOff.label} />
                        <Typography variant="body2">
                          {shortDate(dayKey(nextOff.start))}{nextOff.end > nextOff.start ? ` to ${shortDate(dayKey(nextOff.end))}` : ''}{nextOff.pending ? ' (asked)' : ''}
                        </Typography>
                      </Box>
                    ) : <Typography variant="body2" color="text.secondary">No time off booked.</Typography>}
                  </Box>
                </Paper>
              );
            })}
            <Button variant="outlined" onClick={() => go('add')} sx={{ minHeight: 180, borderStyle: 'dashed', borderRadius: `${theme.shape.borderRadius}px` }}>
              + Add a new hire
            </Button>
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
              <MonthlyBars months={monthly} upTo={today().getMonth()} />
            </Panel>
          </Box>
          <Panel title="By kind" aside={`${year}, approved`}>
            <Box sx={{ display: 'grid', gap: 1.5 }}>
              {(() => {
                const rows = totalsByKind(requests, year);
                const top = Math.max(1, ...rows.map((r) => r[1]));   // the longest bar fills the track
                return rows.map(([label, v, k]) => (
                  <Box key={label} sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, 150px) minmax(0, 1fr) 48px', gap: 1.5, alignItems: 'center' }}>
                    <Typography variant="body2" color="text.secondary">{label}</Typography>
                    <Box sx={{ height: 10, borderRadius: 999, bgcolor: theme.tokens.hover, overflow: 'hidden' }}>
                      <Box
                        sx={{
                          height: '100%', width: `${(v / top) * 100}%`, bgcolor: kindColor(k, theme.palette.mode), borderRadius: 999,
                          transformOrigin: 'left', animation: 'grow .7s both ease-out', '@keyframes grow': { from: { transform: 'scaleX(0)' } },
                          '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
                        }}
                      />
                    </Box>
                    <Typography variant="body2" sx={{ fontWeight: 700, textAlign: 'right' }}>{Math.round(v * 10) / 10}</Typography>
                  </Box>
                ));
              })()}
            </Box>
          </Panel>
          <Panel title="Time to approve">
            <Box sx={{ display: 'grid', gap: 0.75 }}>
              <Typography sx={{ fontFamily: theme.tokens.display, fontSize: 34, fontWeight: 600 }}>
                {approve.avg === null ? 'No approvals yet' : `${approve.avg.toFixed(1)} days`}
              </Typography>
              <Sparkline values={approve.months.map((m) => m.avg)} height={60} />
              <Typography variant="caption" color="text.secondary">
                Average days from a request arriving to its approval over the last 90 days; the line is each of the last six months.
              </Typography>
            </Box>
          </Panel>
        </Box>
      )}

      {tab === 'history' && (
        <Paper sx={{ p: 2 }}>
          <RequestHistory requests={requests} showEmployee />
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

      <Snackbar
        open={snack.open}
        autoHideDuration={4000}
        onClose={() => setSnack((s) => ({ ...s, open: false }))}
        message={snack.message}
      />
    </Box>
  );
}
