import { useCallback, useEffect, useState } from 'react';
import {
  Paper, Typography, Table, TableHead, TableRow, TableCell, TableBody, Button, Alert,
  Stack, Chip, CircularProgress, FormControlLabel, Switch, Box,
} from '@mui/material';
import {
  getHeldRequests, releaseHeldRequest, cancelHeldRequest, getRequestColumns, addRequestColumns,
} from '../api/client';

/** One held request, as /admin/held-requests returns it. */
export interface HeldRow {
  id: number;
  email: string;
  name: string;
  location: string;
  supervisor_name: string;
  request_type: string;
  summary: string;
  status: string;
  created_at: string | null;
  supervisor_reminded_at: string | null;
  admins_notified_at: string | null;
  sp_item_id: string | null;
  last_error: string | null;
}

interface Props {
  /** Whether writes are enabled; actions are disabled in reporting-only mode. */
  processingEnabled: boolean;
  /** Open Add Employee prefilled with this person. */
  onAddEmployee: (row: HeldRow) => void;
}

const STATUS_COLOR: Record<string, 'default' | 'warning' | 'error' | 'success' | 'info'> = {
  held: 'warning', waiting_site: 'info', releasing: 'info', failed: 'error',
  released: 'success', cancelled: 'default',
};
// waiting_site: on staff, not on the SharePoint site yet; retried every hour.
const STATUS_LABEL: Record<string, string> = {
  held: 'Not on staff', waiting_site: 'Waiting for site access', releasing: 'Sending',
  failed: 'Failed', released: 'Sent', cancelled: 'Cancelled',
};
const OPEN_STATUSES = ['held', 'waiting_site', 'failed'];   // matches the backend's OPEN_STATUSES

/**
 * Requests from the request page by people not in the Staff Directory yet.
 * Each waits until its supervisor adds the person; from here an admin can add
 * them (prefilled), retry a release that failed, or cancel a mistake. Also
 * shows whether the request lists have the SubmitterEmail column the page
 * relies on, with a button to add it.
 *
 * @param props - See {@link Props}.
 * @returns The Held Requests panel.
 */
export default function HeldRequests({ processingEnabled, onAddEmployee }: Props) {
  const [rows, setRows] = useState<HeldRow[]>([]);
  const [includeClosed, setIncludeClosed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [message, setMessage] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);

  /** Reload the list. */
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getHeldRequests(includeClosed);
      setRows(res.data.held || []);
    } catch {
      setMessage({ severity: 'error', text: 'Held requests could not be loaded.' });
    } finally {
      setLoading(false);
    }
  }, [includeClosed]);

  useEffect(() => { load(); }, [load]);

  /** Run one row action, then reload. @param id - row. @param action - the call. @param done - success text. */
  const act = async (id: number, action: () => Promise<unknown>, done: string) => {
    setBusyId(id);
    setMessage(null);
    try {
      await action();
      setMessage({ severity: 'success', text: done });
      await load();
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setMessage({ severity: 'error', text: typeof detail === 'string' ? detail : 'That did not work.' });
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Stack spacing={2}>
      <RequestColumnsCard processingEnabled={processingEnabled} />
      <Paper sx={{ p: 3 }}>
        <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 1, flexWrap: 'wrap' }}>
          <Typography variant="h6" sx={{ fontWeight: 600 }}>Held requests</Typography>
          <FormControlLabel
            control={<Switch checked={includeClosed} onChange={(e) => setIncludeClosed(e.target.checked)} />}
            label="Show sent and cancelled"
          />
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Made on the request page by people not in the Staff Directory yet. Adding the person
          sends their requests on. Their supervisor is reminded after 2 business days; admins are
          emailed after 5.
        </Typography>
        {message && <Alert severity={message.severity} sx={{ mb: 2 }}>{message.text}</Alert>}
        {loading ? <CircularProgress size={24} /> : rows.length === 0 ? (
          <Typography color="text.secondary">Nothing is waiting.</Typography>
        ) : (
          <Box sx={{ overflowX: 'auto' }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Person</TableCell>
                  <TableCell>Supervisor</TableCell>
                  <TableCell>Request</TableCell>
                  <TableCell>Since</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell />
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((r) => {
                  const open = OPEN_STATUSES.includes(r.status);
                  return (
                    <TableRow key={r.id}>
                      <TableCell>
                        {r.name}<br />
                        <Typography variant="caption" color="text.secondary">{r.email} · {r.location}</Typography>
                      </TableCell>
                      <TableCell>{r.supervisor_name}</TableCell>
                      <TableCell>{r.summary}</TableCell>
                      <TableCell>{r.created_at?.slice(0, 10)}</TableCell>
                      <TableCell>
                        <Chip size="small" label={STATUS_LABEL[r.status] ?? r.status} color={STATUS_COLOR[r.status] ?? 'default'} />
                        {r.last_error && (
                          <Typography variant="caption" color="error" display="block">{r.last_error}</Typography>
                        )}
                      </TableCell>
                      <TableCell sx={{ whiteSpace: 'nowrap' }}>
                        {open && (
                          <>
                            {r.status === 'held' && (                /* already on staff otherwise */
                              <Button size="small" onClick={() => onAddEmployee(r)}>Add employee</Button>
                            )}
                            <Button size="small" disabled={!processingEnabled || busyId === r.id}
                              onClick={() => act(r.id, () => releaseHeldRequest(r.id), 'Sent on.')}>
                              Retry
                            </Button>
                            <Button size="small" color="error" disabled={busyId === r.id}
                              onClick={() => act(r.id, () => cancelHeldRequest(r.id), 'Cancelled.')}>
                              Cancel
                            </Button>
                          </>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </Box>
        )}
      </Paper>
    </Stack>
  );
}

/**
 * Whether the three request lists have SubmitterEmail and RequestSource, which
 * let a request from the page reach its person without a SharePoint visit.
 *
 * @param props.processingEnabled - Disables the add button in reporting-only mode.
 * @returns A small status card.
 */
function RequestColumnsCard({ processingEnabled }: { processingEnabled: boolean }) {
  const [report, setReport] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    getRequestColumns().then((r) => setReport(r.data)).catch(() => setError('Could not check the request lists.'));
  }, []);

  /** Add any missing column, then show the new report. */
  const add = async () => {
    setBusy(true);
    setError('');
    try {
      setReport((await addRequestColumns()).data);
    } catch {
      setError('The columns could not be added. The app may need the Sites.Manage.All permission.');
    } finally {
      setBusy(false);
    }
  };

  if (!report && !error) return null;
  const listErrors = report
    ? Object.entries(report.lists).filter(([, v]: any) => v.error).map(([k, v]: any) => `${k}: ${v.error}`)
    : [];
  return (
    <Paper sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Request page columns</Typography>
      {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}
      {report && (
        <Stack spacing={1} sx={{ mt: 1 }}>
          <Typography variant="body2">
            SubmitterEmail and RequestSource are {report.ready ? 'on all three request lists' : 'missing from some request lists'}.
            Writing them is {report.enabled ? 'on' : 'off'} (REQUEST_EMAIL_COLUMNS_ENABLED).
          </Typography>
          {listErrors.map((e) => <Alert key={e} severity="warning">{e}</Alert>)}
          {!report.ready && (
            <Box>
              <Button size="small" variant="outlined" disabled={!processingEnabled || busy} onClick={add}>
                Add the columns
              </Button>
            </Box>
          )}
          {report.ready && !report.enabled && (
            <Alert severity="info">
              The columns exist. Turn on REQUEST_EMAIL_COLUMNS_ENABLED so new requests are linked by email.
            </Alert>
          )}
        </Stack>
      )}
    </Paper>
  );
}
