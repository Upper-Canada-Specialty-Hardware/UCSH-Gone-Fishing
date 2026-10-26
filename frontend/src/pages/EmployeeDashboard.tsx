import { ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { readLaunchCache, writeLaunchCache } from '../api/launchCache';
import { loadSession, staffCacheKey } from '../api/intake';
import { Alert, Avatar, Box, Button, Chip, Paper, Tab, Tabs, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import KindPill from '../components/KindPill';
import RequestsHistoryTable from '../components/RequestsHistoryTable';
import TakenFrom from '../components/TakenFrom';
import { Kpi, Panel } from '../components/Panels';
import { MonthlyBars } from '../components/manager/Charts';
import { approveSummary, daysOffByMonth } from '../components/manager/teamStats';
import { daysToCarryReset, myKindRows, myYear } from '../components/employee/myStats';
import { STATUS_COLOR } from '../components/dataGridDefaults';
import { canDecide, stateDisplay } from '../requestState';
import { getMyBalances, getMyRequests } from '../api/client';
import { useLiveRequests } from '../hooks/useLiveRequests';
import { LiveEvent } from '../api/events';
import { kindColor, RequestKind } from '../constants/leaveColors';
import { parseDay, today } from '../utils/dates';
import { firstName } from '../utils/personColor';
import { greeting } from '../utils/greeting';
import { daysWaiting, requestKind, requestWhat, requestWhen } from '../utils/requestText';
import { isHourly } from '../utils/employment';
import { CountUp, EASE, Enter, KF, Seen, riseAt, staggerChildren, useArrival, useDirection } from '../components/Motion';
import { EmployeeSkeleton } from '../components/Skeletons';

/** The tabs, keyed by url segment (#/my/<key>). */
const TABS = ['overview', 'year', 'history'] as const;
type TabKey = typeof TABS[number];

/** How many past requests the overview lists before pointing to History. */
const RECENT_ROWS = 5;

/** The monthly chart's legend: label and the kind whose colour it uses. */
const MONTH_LEGEND: [string, RequestKind][] = [['Vacation', 'vacation'], ['Sick', 'sick'], ['Other', 'bereavement']];

/**
 * Round to one decimal for display, dropping a trailing ".0".
 *
 * @param n - The number.
 * @returns e.g. "2.5" or "3".
 */
function fmt(n: number): string {
  return String(Math.round(n * 10) / 10);
}

/**
 * One balance tile: the name, the number, a line saying how it works, and,
 * when there is a yearly entitlement, a bar of how much is left.
 *
 * @param props.label - The balance.
 * @param props.value - Days.
 * @param props.note - The line under it.
 * @param props.of - The yearly entitlement, for the bar; omit for none.
 * @param props.count - Count the number up from 0 (on arrival only).
 * @returns The tile.
 */
function Pot({ label, value, note, of, count }: { label: string; value: number; note: string; of?: number; count?: boolean }) {
  const theme = useTheme();
  // Share left of the entitlement, kept between 0 and 1 for the bar.
  const left = of && of > 0 ? Math.max(0, Math.min(1, value / of)) : null;
  return (
    <Paper sx={{ p: 2, display: 'grid', gap: 0.5, alignContent: 'start' }}>
      <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
      <Typography sx={{ fontFamily: theme.tokens.display, fontSize: 32, fontWeight: 600, lineHeight: 1.15, color: value < 0 ? 'error.main' : 'text.primary' }}>
        {count ? <CountUp from={0} value={value} format={fmt} /> : fmt(value)}
      </Typography>
      {left !== null && (
        // Low (under a fifth left) turns the bar to the warn colour. The bar fills in;
        // a low one then pulses its warn colour once.
        <Box sx={{ height: 6, borderRadius: 999, bgcolor: theme.tokens.hover, overflow: 'hidden' }} aria-hidden>
          <Box sx={{
            height: '100%', width: `${left * 100}%`, borderRadius: 999, bgcolor: left < 0.2 ? 'warning.main' : 'primary.main',
            transformOrigin: 'left',
            animation: `${KF.growX} .7s ${EASE.out} .12s both${left < 0.2 ? `, ${KF.pulse} .9s ease-in-out 1s` : ''}`,
          }} />
        </Box>
      )}
      <Typography variant="caption" color="text.secondary">{note}</Typography>
    </Paper>
  );
}

/**
 * A counter tile (#300): an hourly person's "vacation taken this year", shown
 * as days with no bar, no "left" and no "of N", so it never reads like a
 * balance. The caption spells out that it only counts up.
 *
 * @param props.label - The tile heading ("Vacation taken").
 * @param props.value - The counted days.
 * @param props.caption - The line under it saying it is a counter.
 * @param props.count - Count the number up from 0 (on arrival only).
 * @returns The tile.
 */
function CounterTile({ label, value, caption, count }: { label: string; value: number; caption: string; count?: boolean }) {
  const theme = useTheme();                                     // for the display serif
  return (
    <Paper sx={{ p: 2, display: 'grid', gap: 0.5, alignContent: 'start' }}>
      <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
      <Typography sx={{ fontFamily: theme.tokens.display, fontSize: 32, fontWeight: 600, lineHeight: 1.15 }}>
        {count ? <CountUp from={0} value={value} format={fmt} /> : fmt(value)}
        {/* "days" sits small beside the number: a count, not a balance figure. */}
        <Box component="span" sx={{ fontSize: 15, fontWeight: 600, color: 'text.secondary', ml: 0.5 }}>days</Box>
      </Typography>
      <Typography variant="caption" color="text.secondary">{caption}</Typography>
    </Paper>
  );
}

/**
 * A titled list of the employee's requests.
 *
 * @param props.title - The heading.
 * @param props.rows - The requests.
 * @param props.empty - What to say when there are none.
 * @returns The section.
 */
function Section({ title, rows, empty }: { title: string; rows: any[]; empty: string }) {
  return (
    <Box>
      <Typography sx={{ fontWeight: 700, px: 2.25, pt: 2, pb: 0.5 }}>{title}</Typography>
      {rows.length === 0 ? (
        <Typography variant="body2" color="text.secondary" sx={{ px: 2.25, pb: 1.5 }}>{empty}</Typography>
      ) : rows.map((r, i) => {
        const waited = r.Status === 'Pending' ? daysWaiting(r) : null;
        // A pending row that has not reached "waiting on approval" yet (getting
        // ready / needs attention) shows that state instead of a waiting count,
        // so the employee sees it is still being set up (#455). SharePoint rows
        // carry no state, so they keep the waiting count.
        const notReady = r.Status === 'Pending' && !canDecide(r.state) ? stateDisplay(r.state) : null;
        // A waiting request says how long it has waited; the rest show their status.
        const label = notReady ? notReady.label
          : r.Status === 'Pending' ? (waited === 0 ? 'Sent today' : `Waiting ${waited} day${waited === 1 ? '' : 's'}`)
          : r.Status || 'Pending';
        // The chip colour follows the state when not ready, else the status map.
        const chipColor = notReady ? notReady.color : (STATUS_COLOR[r.Status] || 'default');
        return (
          <Box key={`${r.request_type}-${r.id}`} sx={{ display: 'flex', alignItems: 'center', gap: 1.25, px: 2.25, py: 1.25, borderTop: 1, borderColor: 'divider', flexWrap: 'wrap', ...riseAt(i, { delay: 120, dur: 340 }) }}>
            <KindPill kind={requestKind(r)} label={requestWhat(r)} />
            <Typography variant="body2" sx={{ flex: 1, minWidth: 120 }}>{requestWhen(r)}</Typography>
            {/* The one thing still open breathes twice, then rests. */}
            <Chip size="small" label={label} color={chipColor}
              sx={{ animation: r.Status === 'Pending' ? `${KF.pulse} 1.4s ease-in-out .7s 2` : 'none' }} />
            {/* Which balances this request's days came from, on its own line under the request. */}
            {r.taken_from && <Box sx={{ flexBasis: '100%' }}><TakenFrom value={r.taken_from} compact /></Box>}
          </Box>
        );
      })}
    </Box>
  );
}

/**
 * One labelled value in the header's record (a dt/dd pair inside the dl).
 *
 * @param props.label - The field's name, e.g. "Department".
 * @param props.children - The value.
 * @returns The pair.
 */
function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography component="dt" variant="caption" color="text.secondary" sx={{ fontWeight: 700, letterSpacing: 0.4, textTransform: 'uppercase' }}>
        {label}
      </Typography>
      {/* dd carries a browser margin; zero it so values line up under their labels. */}
      <Box component="dd" sx={{ m: 0, mt: 0.25, overflowWrap: 'anywhere' }}>{children}</Box>
    </Box>
  );
}

/**
 * A person's initials for their chip, e.g. "Jordan Lee" gives "JL".
 *
 * @param name - The display name.
 * @returns Up to two capital letters.
 */
function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).map((w) => w[0]).slice(0, 2).join('').toUpperCase();
}

/**
 * The employee dashboard, in three tabs kept in the url:
 * Overview (all five balances, what is waiting, coming up and recent),
 * This year (days off by month and by kind, overtime earned, carry-over and
 * payout, how fast requests get approved) and History (every request, with
 * the audit trail). This is My requests at #/my; Make a request opens
 * #/my/request with the same sign-in, so no email code is needed.
 *
 * @returns The employee dashboard.
 */
/** Everything the employee dashboard loads at launch, as saved for the next launch. */
interface EmployeeSnapshot {
  balances: any;
  employee: any;
  requests: any[];
}

export default function EmployeeDashboard() {
  const theme = useTheme();
  const navigate = useNavigate();
  const params = useParams();
  // The open tab comes from the url, so a bookmark or the back button lands on it.
  const tab: TabKey = TABS.includes(params.tab as TabKey) ? (params.tab as TabKey) : 'overview';
  const go = useCallback((k: TabKey) => navigate(`/my/${k}`), [navigate]);
  const dir = useDirection(TABS.indexOf(tab));          // which way a tab change slides
  const arriving = useArrival();                        // numbers count up on arrival only

  const [balances, setBalances] = useState<any>(null);
  const [employee, setEmployee] = useState<any>(null);
  const [requests, setRequests] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Live updates: merge each timeline event into the loaded requests, so an
  // approval shows here without a reload (#328). A decision by email, SMS or an
  // admin also moves this person's balances and the Taken-from chips, which the
  // merge cannot know, so a balance-changing event refetches the balances and
  // requests on a short debounce. Opens once the data has loaded.
  const liveRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onLiveEvent = useCallback((ev: LiveEvent) => {
    const balanceTypes = ['approved', 'rejected', 'refunded', 'cancelled'];   // the ones that move a balance
    if (!balanceTypes.includes(ev.type)) return;
    if (liveRefresh.current) clearTimeout(liveRefresh.current);
    liveRefresh.current = setTimeout(() => {
      Promise.all([getMyBalances(), getMyRequests()])
        .then(([balRes, reqRes]) => {
          setBalances(balRes.data.balances);               // fresh pots after the decision
          setRequests(reqRes.data.requests || []);         // fresh Taken-from chips and status
        })
        .catch(() => { /* the merged rows stay; balances refresh on the next event */ });
    }, 400);
  }, []);
  // Their own staff record changed (an admin edit, a balance correction): re-read
  // the balances and profile. Another person's change is ignored.
  const onStaffChange = useCallback((staffId: string) => {
    if (staffId !== String(loadSession()?.uid ?? '')) return;
    getMyBalances()
      .then((r) => { setBalances(r.data.balances); setEmployee(r.data.employee); })
      .catch(() => { /* the figures stay until the next change */ });
  }, []);
  useLiveRequests({ scope: 'me', setRows: setRequests, enabled: !loading, onEvent: onLiveEvent, onStaffChange });

  useEffect(() => {
    let cancelled = false;
    const cacheKey = staffCacheKey('me');               // "me:<role>:<id>", or null when signed out
    let painted = false;                                // a saved snapshot is already on screen

    /** Put one snapshot of the launch data on screen. */
    const show = (s: EmployeeSnapshot) => {
      setBalances(s.balances);
      setEmployee(s.employee);
      setRequests(s.requests);
      setLoading(false);
    };

    const load = async () => {
      // A relaunch paints the last data at once; the fresh load below replaces it.
      const cached = cacheKey ? await readLaunchCache<EmployeeSnapshot>(cacheKey) : null;
      // Only a snapshot of this view's shape is shown; anything else waits for the fresh load.
      const usable = cached && cached.balances && cached.employee && Array.isArray(cached.requests);
      if (usable && !cancelled) { show(cached); painted = true; }
      try {
        const [balRes, reqRes] = await Promise.all([getMyBalances(), getMyRequests()]);
        if (cancelled) return;
        const fresh: EmployeeSnapshot = {
          balances: balRes.data.balances,
          employee: balRes.data.employee,
          requests: reqRes.data.requests || [],
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

  // The requests in three groups for the overview.
  const groups = useMemo(() => {
    const t = today();
    const waiting = requests.filter((r) => r.Status === 'Pending');
    // Approved time away that has not ended yet, soonest first.
    const coming = requests
      .filter((r) => r.Status === 'Approved' && r.request_type === 'leave' && (parseDay(r.EndDate || r.StartDate) ?? t) >= t)
      .sort((a, b) => String(a.StartDate).localeCompare(String(b.StartDate)));
    const comingKeys = new Set(coming.map((r) => `${r.request_type}-${r.id}`));
    // Everything else, newest first; the overview shows the latest few.
    const past = requests
      .filter((r) => r.Status !== 'Pending' && !comingKeys.has(`${r.request_type}-${r.id}`))
      .sort((a, b) => String(b.StartDate || b.Created || '').localeCompare(String(a.StartDate || a.Created || '')));
    return { waiting, coming, past };
  }, [requests]);

  const year = today().getFullYear();
  // This year's numbers, the monthly chart and the approval speed, all from the employee's own requests.
  const mine = useMemo(() => myYear(requests, year), [requests, year]);
  const monthly = useMemo(() => daysOffByMonth(requests, year), [requests, year]);
  const approve = useMemo(() => approveSummary(requests), [requests]);

  /** Open a new request; the request page uses the same sign-in. */
  const makeRequest = () => navigate('/my/request');

  if (loading) return <EmployeeSkeleton tab={tab} />;
  if (error) {
    return <Alert severity="error" sx={{ m: 2 }}>{error}</Alert>;
  }

  const b = balances || {};
  const hourlyStaff = isHourly(b);                       // hourly: Sick plus the vacation counter only (#300)
  const resetIn = daysToCarryReset();
  // The "by kind" bars: the longest bar fills the track.
  const kindRows = myKindRows(mine);
  const top = Math.max(1, ...kindRows.map((r) => r[1]));
  // The header's record: [label, value]; an empty value shows "Not set".
  const details: [string, string][] = [
    ['Department', employee?.department || ''],
    ['Location', employee?.location || ''],
    ['Employment type', employee?.employment_type || ''],   // Salary or Hourly, as Add Employee calls it
    ['Email', employee?.email || ''],
  ];
  // The backend sends managers as one "A, B" string; split it for the chips.
  const managers = String(employee?.managers || '').split(',').map((s) => s.trim()).filter(Boolean);
  return (
    <Box sx={{ display: 'grid', gap: 2.5, animation: `${KF.fade} .24s ease-out both` }}>
      <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flex: 1, minWidth: 220 }}>
          {/* Same greeting as the manager and admin dashboards, by this device's clock. */}
          <Typography variant="h4" sx={riseAt(0)}>{greeting()}{employee?.name ? `, ${firstName(employee.name)}` : ''}</Typography>
          {/* The person's Staff Directory record, one labelled value each, settling in after the greeting. */}
          <Box component="dl" sx={{ m: 0, mt: 1.25, display: 'flex', flexWrap: 'wrap', columnGap: 3.5, rowGap: 1.25, ...staggerChildren({ delay: 60 }) }}>
            {details.map(([label, value]) => (
              <Detail key={label} label={label}>
                <Typography variant="body2" sx={{ fontWeight: 600, color: value ? 'text.primary' : 'text.secondary' }}>
                  {value || 'Not set'}
                </Typography>
              </Detail>
            ))}
            {/* Everyone who approves this person's requests, one chip each. */}
            <Detail label={managers.length === 1 ? 'Manager' : 'Managers'}>
              {managers.length ? (
                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75 }}>
                  {managers.map((m) => (
                    <Chip key={m} size="small" variant="outlined" label={m}
                      avatar={<Avatar sx={{ bgcolor: 'primary.main', '&&': { color: 'primary.contrastText', fontWeight: 700 } }}>{initials(m)}</Avatar>} />
                  ))}
                </Box>
              ) : (
                <Typography variant="body2" color="text.secondary">None set yet. Ask HR to add one.</Typography>
              )}
            </Detail>
          </Box>
        </Box>
        <Button variant="contained" size="large" onClick={makeRequest} sx={riseAt(3, { step: 60 })}>Make a request</Button>
      </Box>

      <Tabs value={tab} onChange={(_, v) => go(v)} variant="scrollable" allowScrollButtonsMobile sx={{ borderBottom: 1, borderColor: 'divider' }}>
        <Tab value="overview" label={groups.waiting.length ? `Overview (${groups.waiting.length} waiting)` : 'Overview'} />
        <Tab value="year" label="This year" />
        <Tab value="history" label={`History (${requests.length})`} />
      </Tabs>

      {/* The tab's body slides in from the side of the tab picked. */}
      <Enter key={tab} dir={dir} sx={{ display: 'grid', gap: 2.5, minWidth: 0 }}>
      {tab === 'overview' && (
        <>
          {/* The balances, each with how it works in a line; they arrive in reading order.
              hourly people have only Sick plus the vacation counter (#300). */}
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(min(160px, 100%), 1fr))', ...staggerChildren({ step: 40 }) }}>
            {hourlyStaff ? (
              <>
                {/* Vacation is a counter here, not a balance: no bar, no "left". */}
                <CounterTile count={arriving} label="Vacation taken" value={b.vacation_taken ?? 0} caption="This year. Counted, not a balance." />
                <Pot count={arriving} label="Sick or personal" value={b.sick_balance ?? 0} of={b.sick_entitlement} note={b.sick_entitlement ? `left of ${fmt(b.sick_entitlement)} days` : 'days left'} />
              </>
            ) : (
              <>
                <Pot count={arriving} label="Vacation" value={b.vacation_balance ?? 0} of={b.vacation_entitlement} note={b.vacation_entitlement ? `left of ${fmt(b.vacation_entitlement)} days this year` : 'days left'} />
                <Pot count={arriving} label="Sick or personal" value={b.sick_balance ?? 0} of={b.sick_entitlement} note={b.sick_entitlement ? `left of ${fmt(b.sick_entitlement)} days` : 'days left'} />
                <Pot count={arriving} label="Make-Up" value={b.overtime ?? 0} note="earned from overtime, used first for vacation" />
                <Pot count={arriving} label="Carry Over" value={b.carryover ?? 0} note={`used before vacation, cleared April 1 (in ${resetIn} day${resetIn === 1 ? '' : 's'})`} />
                <Pot count={arriving} label="Payout" value={b.payout ?? 0} note="days set aside to be paid out" />
              </>
            )}
          </Box>

          <Paper sx={{ overflow: 'hidden', pb: 0.5 }}>
            <Section title="Waiting on your manager" rows={groups.waiting} empty="Nothing waiting." />
            <Section title="Coming up" rows={groups.coming} empty="No approved time off coming up." />
            <Section title="Recent" rows={groups.past.slice(0, RECENT_ROWS)} empty="Nothing yet." />
            {groups.past.length > RECENT_ROWS && (
              <Box sx={{ px: 2.25, py: 1.5, borderTop: 1, borderColor: 'divider' }}>
                <Button size="small" onClick={() => go('history')}>See all {requests.length} requests in History</Button>
              </Box>
            )}
          </Paper>
        </>
      )}

      {tab === 'year' && (
        <>
          {/* The year at a glance. */}
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(min(160px, 100%), 1fr))', ...staggerChildren({ step: 40 }) }}>
            <Kpi label="Days off" value={fmt(mine.daysOff)} note={`approved, ${year}`} />
            {/* Days taken, not the vacation pot's drop: make-up and carry-over are spent first. */}
            <Kpi label="Vacation taken" value={fmt(mine.vacation)} note="days, part days included" />
            <Kpi label="Sick days used" value={fmt(mine.sick)} note={b.sick_entitlement ? `of ${fmt(b.sick_entitlement)} days` : 'days'} />
            <Kpi label="Overtime earned" value={`${fmt(mine.overtimeHours)} h`} note={`${fmt(mine.overtimeHours / 8)} make-up days`} />
            <Kpi
              label="Time to approve" value={approve.avg === null ? 'None yet' : `${approve.avg.toFixed(1)} d`}
              note="your requests, last 90 days" spark={approve.months.length > 1 ? approve.months.map((m) => m.avg) : undefined}
            />
          </Box>

          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: 'minmax(0, 1.4fr) minmax(0, 1fr)' }, alignItems: 'start' }}>
            <Panel title={`Days off per month, ${year}`} aside="approved leave, by start date">
              <Box sx={{ display: 'flex', gap: 2, mb: 1, fontSize: 13, color: 'text.secondary', flexWrap: 'wrap' }}>
                {MONTH_LEGEND.map(([l, k]) => (
                  <Box key={l} sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                    <Box sx={{ width: 10, height: 10, borderRadius: 0.5, bgcolor: kindColor(k, theme.palette.mode) }} />{l}
                  </Box>
                ))}
              </Box>
              {/* Charts start when they scroll into view. */}
              <Seen><MonthlyBars months={monthly} upTo={today().getMonth()} /></Seen>
            </Panel>

            <Panel title="By kind" aside={`${year}, approved, in days`}>
              <Seen sx={{ display: 'grid', gap: 1.5 }}>
                {kindRows.map(([label, v, k], i) => (
                  <Box key={label} sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, 150px) minmax(0, 1fr) 40px', gap: 1.5, alignItems: 'center' }}>
                    <Typography variant="body2" color="text.secondary">{label}</Typography>
                    <Box sx={{ height: 10, borderRadius: 999, bgcolor: theme.tokens.hover, overflow: 'hidden' }}>
                      <Box
                        sx={{
                          height: '100%', width: `${(v / top) * 100}%`, bgcolor: kindColor(k, theme.palette.mode), borderRadius: 999,
                          transformOrigin: 'left', animation: `${KF.growX} .7s ease-out ${0.15 + i * 0.07}s both`,
                        }}
                      />
                    </Box>
                    <Typography variant="body2" sx={{ fontWeight: 700, textAlign: 'right' }}>{fmt(v)}</Typography>
                  </Box>
                ))}
              </Seen>
            </Panel>
          </Box>
        </>
      )}

      {tab === 'history' && (
        // minWidth 0 lets this item shrink below the table's natural width; the
        // table (RequestsHistoryTable) is the admin requests table in read-only
        // "mine" mode and keeps itself inside the card, never widening the page.
        <Box sx={{ minWidth: 0 }}>
          <RequestsHistoryTable requests={requests} mode="mine" timelineScope="me" />
        </Box>
      )}
      </Enter>
    </Box>
  );
}
