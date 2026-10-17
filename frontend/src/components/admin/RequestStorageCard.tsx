import { useEffect, useState } from 'react';
import { Alert, Box, Button, Paper, Stack, Typography } from '@mui/material';
import { flipStorageToPostgres, getStorageStatus } from '../../api/client';

/** Readable names for the three request kinds in the counts line. */
const KIND_LABELS: Record<string, string> = {
  leave: 'Leave',
  overtime: 'Overtime',
  carryover_payout: 'Carry-over / payout',
};

/** One sync run as the status endpoint returns it. */
interface SyncRun {
  job: string;
  started_at: string | null;
  finished_at: string | null;
  counts: Record<string, any>;
  error: string | null;
}

/** The shape of the GET /admin/storage payload. */
interface StorageStatus {
  requests_storage: 'sharepoint' | 'postgres';
  staff_storage: 'sharepoint' | 'postgres';
  paused: boolean;
  last_sync: SyncRun | null;
  request_counts: Record<string, number>;
  open_findings: number;
}

/**
 * A readable local date and time for a sync timestamp.
 *
 * @param iso - The ISO timestamp, or null when the run never finished.
 * @returns A locale date-time string, or "never" when there is no timestamp.
 */
function fmt(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : 'never';
}

/**
 * Where requests and staff are stored, and the one-step switch to Postgres.
 *
 * Shows the current backend for requests and staff, the last sync time, how many
 * requests are in Postgres and how many findings are open, and a single
 * "Switch to Postgres" action with an in-page confirm (no window.confirm). While
 * the switch runs it shows the pause; afterwards it shows "Already switched". The
 * old JSON copy action and the STORAGE_REQUESTS note are gone: the switch is one
 * deliberate step (#335/#336).
 *
 * @param props.processingEnabled - Writes are off in reporting-only mode, so the
 *   switch (a heavy write) is disabled then.
 * @returns A status-and-action card for the Data checks screen.
 */
export default function RequestStorageCard({ processingEnabled }: { processingEnabled: boolean }) {
  const [status, setStatus] = useState<StorageStatus | null>(null);   // the latest status payload
  const [error, setError] = useState('');                             // a readable error, or empty
  const [confirming, setConfirming] = useState(false);               // the in-page confirm step is open
  const [switching, setSwitching] = useState(false);                 // the flip request is in flight

  /** Load the storage status from the server. */
  const load = () =>
    getStorageStatus()
      .then((r) => setStatus(r.data))
      .catch(() => setError('Could not read the storage status.'));

  useEffect(() => { load(); }, []);

  /** Run the one-step switch, then reload the status to show the result. */
  const doSwitch = async () => {
    setSwitching(true);                                              // disable the buttons while it runs
    setError('');
    try {
      await flipStorageToPostgres();                                // the server pauses writes for its duration
      setConfirming(false);                                         // close the confirm step on success
    } catch (err: any) {
      // A 409 carries "Already switched" or "A sync is running"; show it plainly.
      setError(err?.response?.data?.detail || 'The switch did not run. Try again in a moment.');
    } finally {
      setSwitching(false);
      await load();                                                 // reflect the new storage, pause and counts
    }
  };

  if (!status && !error) return null;                                // nothing to show yet
  const inPostgres = status?.requests_storage === 'postgres';
  const counts = status?.request_counts || {};
  const countsText = Object.entries(counts)
    .map(([kind, n]) => `${KIND_LABELS[kind] || kind}: ${n}`)
    .join(', ');

  return (
    <Paper sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Request storage</Typography>
      {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}
      {status?.paused && (
        <Alert severity="warning" sx={{ mt: 1 }}>
          The system is being updated. Please try again in a minute.
        </Alert>
      )}
      {status && (
        <Stack spacing={1} sx={{ mt: 1 }}>
          <Typography variant="body2">
            Requests are read and written in <b>{inPostgres ? 'Postgres' : 'SharePoint'}</b>; staff in{' '}
            <b>{status.staff_storage === 'postgres' ? 'Postgres' : 'SharePoint'}</b>.
          </Typography>
          {inPostgres && countsText && (
            <Typography variant="body2" color="text.secondary">Requests in Postgres: {countsText}.</Typography>
          )}
          {status.last_sync && (
            <Typography variant="body2" color="text.secondary">
              Last {status.last_sync.job.replace('_', ' ')}: {fmt(status.last_sync.finished_at)}
              {status.last_sync.error ? ` (error: ${status.last_sync.error})` : ''}.
            </Typography>
          )}
          <Typography variant="body2" color="text.secondary">
            {status.open_findings} open finding{status.open_findings === 1 ? '' : 's'} on the Legacy data screen.
          </Typography>

          {inPostgres ? (
            <Alert severity="success">Already switched. Requests now live in Postgres.</Alert>
          ) : confirming ? (
            <Alert severity="warning">
              <Typography variant="body2" sx={{ mb: 1 }}>
                This runs one final sync and moves requests and staff to Postgres. It is one-way: SharePoint
                stops being the source. New submissions, approvals and staff edits are refused for a few
                seconds while it runs.
              </Typography>
              <Stack direction="row" spacing={1}>
                <Button size="small" variant="contained" color="warning" disabled={switching} onClick={doSwitch}>
                  {switching ? 'Switching...' : 'Yes, switch now'}
                </Button>
                <Button size="small" disabled={switching} onClick={() => setConfirming(false)}>Cancel</Button>
              </Stack>
            </Alert>
          ) : (
            <Box>
              <Button
                size="small"
                variant="outlined"
                disabled={!processingEnabled}
                onClick={() => { setError(''); setConfirming(true); }}
              >
                Switch to Postgres
              </Button>
              {!processingEnabled && (
                <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                  Enable processing to switch.
                </Typography>
              )}
            </Box>
          )}
        </Stack>
      )}
    </Paper>
  );
}
