import { useEffect, useState, useCallback } from 'react';
import { Box, Typography, Paper, CircularProgress, Alert, Tabs, Tab, Snackbar, ToggleButtonGroup, ToggleButton } from '@mui/material';
import PendingApprovals from '../components/PendingApprovals';
import TeamBalanceTable from '../components/TeamBalanceTable';
import TeamCalendar from '../components/TeamCalendar';
import TeamTimeline from '../components/TeamTimeline';
import RequestHistory from '../components/RequestHistory';
import AddEmployee from '../components/AddEmployee';
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

export default function ManagerDashboard() {
  const [prefill] = useState(takeAddEmployeePrefill);
  // Opened from an "add this new hire" email: go straight to Add Employee (tab 4).
  const [tab, setTab] = useState(prefill ? 4 : 0);
  const [calendarView, setCalendarView] = useState<'month' | 'timeline'>('timeline');
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
        setMembers(membersRes.data.members || []);
        setPending(pendingRes.data.pending || []);
        setRequests(reqRes.data.requests || []);
        setCalendarEvents(calRes.data.events || []);
        setProcessingEnabled(configRes.data.processing_enabled || false);
      })
      .catch((err) => setError(err.response?.data?.detail || 'Failed to load data'))
      .finally(() => setLoading(false));
  }, []);

  const handleApprove = useCallback(async (type: string, id: string) => {
    const key = `${type}-${id}`;
    setActionLoading(key);
    try {
      await approveRequest(type, id);
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
    const key = `${type}-${id}`;
    setActionLoading(key);
    try {
      await rejectRequest(type, id);
      setPending((prev) => prev.filter((p) => !(p.request_type === type && String(p.id) === String(id))));
      setSnack({ open: true, message: 'Request rejected', severity: 'success' });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'Rejection failed', severity: 'error' });
    } finally {
      setActionLoading(null);
    }
  }, []);

  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
        <CircularProgress />
      </Box>
    );
  }

  if (error) {
    return <Alert severity="error" sx={{ m: 2 }}>{error}</Alert>;
  }

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 3, fontWeight: 600 }}>
        {managerName ? `Welcome, ${managerName}` : 'Team Dashboard'}
      </Typography>

      {!processingEnabled && (
        <Alert severity="info" sx={{ mb: 2 }}>
          System is in reporting-only mode. Approve/reject actions are disabled.
        </Alert>
      )}

      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 3 }}>
        <Tab label={`Pending (${pending.length})`} />
        <Tab label="Team Balances" />
        <Tab label="Calendar" />
        <Tab label="Request History" />
        <Tab label="Add Employee" />
      </Tabs>

      {tab === 0 && (
        <PendingApprovals
          pending={pending}
          processingEnabled={processingEnabled}
          onApprove={handleApprove}
          onReject={handleReject}
          actionLoading={actionLoading}
        />
      )}

      {tab === 1 && (
        <Paper sx={{ p: 3 }}>
          <TeamBalanceTable members={members} />
        </Paper>
      )}

      {tab === 2 && (
        <Paper sx={{ p: 3 }}>
          <ToggleButtonGroup
            value={calendarView}
            exclusive
            onChange={(_, v) => { if (v) setCalendarView(v); }}
            size="small"
            sx={{ mb: 2 }}
          >
            <ToggleButton value="month">Month</ToggleButton>
            <ToggleButton value="timeline">Timeline</ToggleButton>
          </ToggleButtonGroup>
          {calendarView === 'month' ? (
            <TeamCalendar events={calendarEvents} />
          ) : (
            <TeamTimeline events={calendarEvents} />
          )}
        </Paper>
      )}

      {tab === 3 && (
        <Paper sx={{ p: 3 }}>
          <RequestHistory requests={requests} showEmployee />
        </Paper>
      )}

      {tab === 4 && (
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
