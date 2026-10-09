import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert, Box, Button, Chip, CircularProgress, Paper, Tab, Tabs, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import KindPill from '../components/KindPill';
import RequestHistory from '../components/RequestHistory';
import { Kpi, Panel } from '../components/Panels';
import { MonthlyBars } from '../components/manager/Charts';
import { approveSummary, daysOffByMonth } from '../components/manager/teamStats';
import { daysToCarryReset, myKindRows, myYear } from '../components/employee/myStats';
import { STATUS_COLOR } from '../components/dataGridDefaults';
import { getMyBalances, getMyRequests } from '../api/client';
import { handOffSession } from '../api/intake';
import { kindColor, RequestKind } from '../constants/leaveColors';
import { parseDay, today } from '../utils/dates';
import { firstName } from '../utils/personColor';
import { daysWaiting, requestKind, requestWhat, requestWhen } from '../utils/requestText';

/** The tabs, keyed by url segment (#/employee/<key>). */
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
 * @returns The tile.
 */
function Pot({ label, value, note, of }: { label: string; value: number; note: string; of?: number }) {
  const theme = useTheme();
  // Share left of the entitlement, kept between 0 and 1 for the bar.
  const left = of && of > 0 ? Math.max(0, Math.min(1, value / of)) : null;
  return (
    <Paper sx={{ p: 2, display: 'grid', gap: 0.5, alignContent: 'start' }}>
      <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
      <Typography sx={{ fontFamily: theme.tokens.display, fontSize: 32, fontWeight: 600, lineHeight: 1.15, color: value < 0 ? 'error.main' : 'text.primary' }}>
        {fmt(value)}
      </Typography>
      {left !== null && (
        // Low (under a fifth left) turns the bar to the warn colour.
        <Box sx={{ height: 6, borderRadius: 999, bgcolor: theme.tokens.hover, overflow: 'hidden' }} aria-hidden>
          <Box sx={{ height: '100%', width: `${left * 100}%`, borderRadius: 999, bgcolor: left < 0.2 ? 'warning.main' : 'primary.main' }} />
        </Box>
      )}
      <Typography variant="caption" color="text.secondary">{note}</Typography>
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
      ) : rows.map((r) => {
        const waited = r.Status === 'Pending' ? daysWaiting(r) : null;
        // A waiting request says how long it has waited; the rest show their status.
        const label = r.Status === 'Pending' ? (waited === 0 ? 'Sent today' : `Waiting ${waited} day${waited === 1 ? '' : 's'}`) : r.Status || 'Pending';
        return (
          <Box key={`${r.request_type}-${r.id}`} sx={{ display: 'flex', alignItems: 'center', gap: 1.25, px: 2.25, py: 1.25, borderTop: 1, borderColor: 'divider', flexWrap: 'wrap' }}>
            <KindPill kind={requestKind(r)} label={requestWhat(r)} />
            <Typography variant="body2" sx={{ flex: 1, minWidth: 120 }}>{requestWhen(r)}</Typography>
            <Chip size="small" label={label} color={STATUS_COLOR[r.Status] || 'default'} />
          </Box>
        );
      })}
    </Box>
  );
}

/**
 * The employee dashboard, in three tabs kept in the url:
 * Overview (all five balances, what is waiting, coming up and recent),
 * This year (days off by month and by kind, overtime earned, carry-over and
 * payout, how fast requests get approved) and History (every request, with
 * the audit trail). Make a request opens the request page already signed in:
 * the dashboard's own signed token is handed to the request page for this tab
 * (see handOffSession), so no email code is needed.
 *
 * @returns The employee dashboard.
 */
export default function EmployeeDashboard() {
  const theme = useTheme();
  const navigate = useNavigate();
  const params = useParams();
  // The open tab comes from the url, so a bookmark or the back button lands on it.
  const tab: TabKey = TABS.includes(params.tab as TabKey) ? (params.tab as TabKey) : 'overview';
  const go = useCallback((k: TabKey) => navigate(`/employee/${k}`), [navigate]);

  const [balances, setBalances] = useState<any>(null);
  const [employee, setEmployee] = useState<any>(null);
  const [requests, setRequests] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    Promise.all([getMyBalances(), getMyRequests()])
      .then(([balRes, reqRes]) => {
        setBalances(balRes.data.balances);
        setEmployee(balRes.data.employee);
        setRequests(reqRes.data.requests || []);
      })
      .catch((err) => setError(err.response?.data?.detail || 'Failed to load data'))
      .finally(() => setLoading(false));
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

  /** Hand this dashboard's sign-in to the request page and open it at the form. */
  const makeRequest = () => {
    const token = sessionStorage.getItem('dashboard_token');
    const role = sessionStorage.getItem('dashboard_role');
    const uid = sessionStorage.getItem('dashboard_uid');
    const exp = sessionStorage.getItem('dashboard_exp');
    // Without a complete token the request page simply asks for an email code.
    if (token && role && uid && exp && employee) {
      handOffSession({ token, role, uid, exp, name: employee.name || '', email: employee.email || '' });
    }
    navigate('/request');
  };

  if (loading) {
    return <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}><CircularProgress /></Box>;
  }
  if (error) {
    return <Alert severity="error" sx={{ m: 2 }}>{error}</Alert>;
  }

  const b = balances || {};
  const resetIn = daysToCarryReset();
  // The "by kind" bars: the longest bar fills the track.
  const kindRows = myKindRows(mine);
  const top = Math.max(1, ...kindRows.map((r) => r[1]));
  return (
    <Box sx={{ display: 'grid', gap: 2.5 }}>
      <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flex: 1, minWidth: 220 }}>
          <Typography variant="h4">Hi {firstName(employee?.name || '') || 'there'}</Typography>
          <Typography color="text.secondary">
            {[employee?.department, employee?.location].filter(Boolean).join(' · ')}
          </Typography>
          {/* Who approves this person's requests, from the Staff Directory. */}
          <Typography variant="body2" color="text.secondary">
            {employee?.managers ? `Your requests go to ${employee.managers}` : 'No manager is set for you yet. Ask HR to add one.'}
          </Typography>
        </Box>
        <Button variant="contained" size="large" onClick={makeRequest}>Make a request</Button>
      </Box>

      <Tabs value={tab} onChange={(_, v) => go(v)} variant="scrollable" allowScrollButtonsMobile sx={{ borderBottom: 1, borderColor: 'divider' }}>
        <Tab value="overview" label={groups.waiting.length ? `Overview (${groups.waiting.length} waiting)` : 'Overview'} />
        <Tab value="year" label="This year" />
        <Tab value="history" label={`History (${requests.length})`} />
      </Tabs>

      {tab === 'overview' && (
        <>
          {/* All five balances, each with how it works in a line. */}
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(min(160px, 100%), 1fr))' }}>
            <Pot label="Vacation" value={b.vacation_balance ?? 0} of={b.vacation_entitlement} note={b.vacation_entitlement ? `left of ${fmt(b.vacation_entitlement)} days this year` : 'days left'} />
            <Pot label="Sick or personal" value={b.sick_balance ?? 0} of={b.sick_entitlement} note={b.sick_entitlement ? `left of ${fmt(b.sick_entitlement)} days` : 'days left'} />
            <Pot label="Make-Up" value={b.overtime ?? 0} note="earned from overtime, used first for vacation" />
            <Pot label="Carry Over" value={b.carryover ?? 0} note={`used before vacation, cleared April 1 (in ${resetIn} day${resetIn === 1 ? '' : 's'})`} />
            <Pot label="Payout" value={b.payout ?? 0} note="days set aside to be paid out" />
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
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(min(160px, 100%), 1fr))' }}>
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
              <MonthlyBars months={monthly} upTo={today().getMonth()} />
            </Panel>

            <Panel title="By kind" aside={`${year}, approved, in days`}>
              <Box sx={{ display: 'grid', gap: 1.5 }}>
                {kindRows.map(([label, v, k]) => (
                  <Box key={label} sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, 150px) minmax(0, 1fr) 40px', gap: 1.5, alignItems: 'center' }}>
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
                    <Typography variant="body2" sx={{ fontWeight: 700, textAlign: 'right' }}>{fmt(v)}</Typography>
                  </Box>
                ))}
              </Box>
            </Panel>
          </Box>
        </>
      )}

      {tab === 'history' && (
        // minWidth 0 and overflow hidden keep the table scrolling inside the card, never the page.
        <Paper sx={{ p: 2, minWidth: 0, overflow: 'hidden' }}>
          <RequestHistory requests={requests} />
        </Paper>
      )}
    </Box>
  );
}
