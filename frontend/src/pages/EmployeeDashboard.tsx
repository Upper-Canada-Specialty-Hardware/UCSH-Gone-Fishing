import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Box, Button, Chip, CircularProgress, Paper, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import KindPill from '../components/KindPill';
import RequestHistory from '../components/RequestHistory';
import { STATUS_COLOR } from '../components/dataGridDefaults';
import { getMyBalances, getMyRequests } from '../api/client';
import { handOffSession } from '../api/intake';
import { parseDay, today } from '../utils/dates';
import { firstName } from '../utils/personColor';
import { daysWaiting, requestKind, requestWhat, requestWhen } from '../utils/requestText';

/**
 * One balance tile: the name, the number, and a line saying how it works.
 *
 * @param props.label - The balance.
 * @param props.value - Days.
 * @param props.note - The line under it.
 * @returns The tile.
 */
function Pot({ label, value, note }: { label: string; value: number; note: string }) {
  const theme = useTheme();
  return (
    <Paper sx={{ p: 2, display: 'grid', gap: 0.25 }}>
      <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
      <Typography sx={{ fontFamily: theme.tokens.display, fontSize: 32, fontWeight: 600, lineHeight: 1.15, color: value < 0 ? 'error.main' : 'text.primary' }}>
        {value}
      </Typography>
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
 * The employee dashboard: a greeting with a Make a request button, the four
 * balances with a line on how each works, then the employee's requests in
 * three groups (waiting on the manager, coming up, past) with the full table
 * one click away.
 *
 * Make a request opens the request page at the form, already signed in: the
 * dashboard's own signed token is handed to the request page for this tab
 * (see handOffSession), so no email code is needed.
 *
 * @returns The employee dashboard.
 */
export default function EmployeeDashboard() {
  const navigate = useNavigate();
  const [balances, setBalances] = useState<any>(null);
  const [employee, setEmployee] = useState<any>(null);
  const [requests, setRequests] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showAll, setShowAll] = useState(false);

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

  // The requests in three groups.
  const groups = useMemo(() => {
    const t = today();
    const waiting = requests.filter((r) => r.Status === 'Pending');
    // Approved time away that has not ended yet, soonest first.
    const coming = requests
      .filter((r) => r.Status === 'Approved' && r.request_type === 'leave' && (parseDay(r.EndDate || r.StartDate) ?? t) >= t)
      .sort((a, b) => String(a.StartDate).localeCompare(String(b.StartDate)));
    const comingKeys = new Set(coming.map((r) => `${r.request_type}-${r.id}`));
    // Everything else, newest first, the latest eight.
    const past = requests
      .filter((r) => r.Status !== 'Pending' && !comingKeys.has(`${r.request_type}-${r.id}`))
      .sort((a, b) => String(b.StartDate || b.Created || '').localeCompare(String(a.StartDate || a.Created || '')))
      .slice(0, 8);
    return { waiting, coming, past };
  }, [requests]);

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
  return (
    <Box sx={{ display: 'grid', gap: 2.5 }}>
      <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flex: 1, minWidth: 220 }}>
          <Typography variant="h4">Hi {firstName(employee?.name || '') || 'there'}</Typography>
          <Typography color="text.secondary">
            {[employee?.department, employee?.location].filter(Boolean).join(' · ')}
          </Typography>
        </Box>
        <Button variant="contained" size="large" onClick={makeRequest}>Make a request</Button>
      </Box>

      {/* The four balances, each with how it works in a line. */}
      <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(min(150px, 100%), 1fr))' }}>
        <Pot label="Vacation" value={b.vacation_balance ?? 0} note={b.vacation_entitlement ? `of ${b.vacation_entitlement} days this year` : 'days'} />
        <Pot label="Sick" value={b.sick_balance ?? 0} note={b.sick_entitlement ? `of ${b.sick_entitlement} days` : 'days'} />
        <Pot label="Make-Up" value={b.overtime ?? 0} note="used first for vacation" />
        <Pot label="Carry Over" value={b.carryover ?? 0} note="resets April 1" />
      </Box>

      <Paper sx={{ overflow: 'hidden', pb: 0.5 }}>
        <Section title="Waiting on your manager" rows={groups.waiting} empty="Nothing waiting." />
        <Section title="Coming up" rows={groups.coming} empty="No approved time off coming up." />
        <Section title="Past" rows={groups.past} empty="Nothing yet." />
        {requests.length > 0 && (
          <Box sx={{ px: 2.25, py: 1.5, borderTop: 1, borderColor: 'divider' }}>
            <Button size="small" onClick={() => setShowAll((v) => !v)}>{showAll ? 'Hide the full list' : 'See every request'}</Button>
          </Box>
        )}
      </Paper>

      {showAll && (
        <Paper sx={{ p: 2 }}>
          <RequestHistory requests={requests} />
        </Paper>
      )}
    </Box>
  );
}
