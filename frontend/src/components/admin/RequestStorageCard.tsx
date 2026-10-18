import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, CircularProgress, Paper, Stack, Typography } from '@mui/material';
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

/** The per-step counts of the final sync, including any errors and error ids. */
type FinalSync = Record<string, any>;

/** The background switch's progress, added to GET /admin/storage (new contract). */
interface SwitchInfo {
  state: 'idle' | 'running' | 'done' | 'failed';
  /** pausing / waiting_in_flight / final_sync / switching / done. */
  step: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  final_sync: FinalSync | null;
}

/** The shape of the GET /admin/storage payload. */
interface StorageStatus {
  requests_storage: 'sharepoint' | 'postgres';
  staff_storage: 'sharepoint' | 'postgres';
  paused: boolean;
  last_sync: SyncRun | null;
  request_counts: Record<string, number>;
  open_findings: number;
  /** The background switch's progress; absent on the old response shape. */
  switch?: SwitchInfo | null;
}

/** The switch steps in plain words, in the order they run. */
const STEP_LABELS: Record<string, string> = {
  pausing: 'Pausing new requests and decisions',
  waiting_in_flight: 'Waiting for decisions already in progress',
  final_sync: 'Copying the latest SharePoint data',
  switching: 'Switching to Postgres',
  done: 'Done',
};

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
 * A snake_case key in plain words, e.g. ``requests`` to "Requests".
 *
 * @param key - The key.
 * @returns The humanised label.
 */
function humanize(key: string): string {
  const s = key.replace(/_/g, ' ').trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * The final-sync counts as readable lines, one per step, never a raw blob.
 *
 * @param fs - The final_sync object, or null.
 * @returns One readable line per step with its counts.
 */
function finalSyncLines(fs: FinalSync | null): string[] {
  if (!fs) return [];
  return Object.entries(fs).map(([step, value]) => {
    if (value && typeof value === 'object') {
      const parts = Object.entries(value as Record<string, unknown>)
        .filter(([k]) => k !== 'error_ids')               // ids are listed separately
        .map(([k, v]) => `${humanize(k)} ${Array.isArray(v) ? v.length : v}`);
      return `${humanize(step)}: ${parts.join(', ')}`;
    }
    return `${humanize(step)}: ${value}`;
  });
}

/**
 * Every item id the final sync could not copy, gathered from any error_ids list
 * (top level or per step), deduped.
 *
 * @param fs - The final_sync object, or null.
 * @returns The ids as strings, or an empty list.
 */
function finalSyncErrorIds(fs: FinalSync | null): string[] {
  if (!fs) return [];
  const ids: string[] = [];
  const scan = (v: unknown) => { if (Array.isArray(v)) ids.push(...v.map(String)); };
  scan((fs as Record<string, unknown>).error_ids);        // top level
  for (const v of Object.values(fs)) {
    if (v && typeof v === 'object') scan((v as Record<string, unknown>).error_ids);   // per step
  }
  return Array.from(new Set(ids));
}

/**
 * Where requests and staff are stored, and the one-step switch to Postgres.
 *
 * Shows the current backend for requests and staff, the last sync time, how many
 * requests are in Postgres and how many findings are open, and a single
 * "Switch to Postgres" action with an in-page confirm (no window.confirm). The
 * switch runs in the background: after confirming, the card polls the status
 * every two seconds and names the current step in plain words (pausing, waiting
 * for decisions in flight, copying the data, switching), then shows the final
 * sync counts, or the error and any affected item ids if it failed. The button
 * stays disabled while it runs, and polling resumes if the card is opened
 * mid-switch. The old JSON copy action and the STORAGE_REQUESTS note are gone:
 * the switch is one deliberate step (#335/#336).
 *
 * @param props.processingEnabled - Writes are off in reporting-only mode, so the
 *   switch (a heavy write) is disabled then.
 * @returns A status-and-action card for the Data checks screen.
 */
export default function RequestStorageCard({ processingEnabled }: { processingEnabled: boolean }) {
  const [status, setStatus] = useState<StorageStatus | null>(null);   // the latest status payload
  const [error, setError] = useState('');                             // a readable error, or empty
  const [confirming, setConfirming] = useState(false);                // the in-page confirm step is open
  const [starting, setStarting] = useState(false);                    // the POST is in flight, before polling takes over

  /** Load the storage status; returns it so a caller can read the result. */
  const load = useCallback(async (): Promise<StorageStatus | null> => {
    try {
      const r = await getStorageStatus();
      setStatus(r.data);
      return r.data as StorageStatus;
    } catch {
      setError('Could not read the storage status.');
      return null;
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const sw = status?.switch || null;                                  // the background switch's progress, if any
  const running = sw?.state === 'running';                            // a switch is under way

  // While a switch runs (started here, in another tab, or before this card was
  // opened), poll every 2s so the step and the final result follow it. Resumes
  // on its own when the card is opened mid-switch, since `running` reads status.
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => void load(), 2000);
    return () => clearInterval(t);
  }, [running, load]);

  /** Start the switch. The server runs it in the background; the poll shows progress. */
  const doSwitch = async () => {
    setStarting(true);                                                // disable the button until polling takes over
    setError('');
    try {
      // New contract: 202 { started: true }. Old contract: 200 with the final
      // result. Either way the reload below shows it: a running switch, or the
      // finished state for the old shape.
      await flipStorageToPostgres();
    } catch (err: any) {
      // 409 means a switch is already running, or it is already switched.
      setError(err?.response?.data?.detail || 'The switch did not start. Try again in a moment.');
    } finally {
      await load();                                                   // pick up the running switch or the result
      setConfirming(false);
      setStarting(false);
    }
  };

  if (!status && !error) return null;                                 // nothing to show yet
  const inPostgres = status?.requests_storage === 'postgres';
  const counts = status?.request_counts || {};
  const countsText = Object.entries(counts)
    .map(([kind, n]) => `${KIND_LABELS[kind] || kind}: ${n}`)
    .join(', ');
  const syncLines = finalSyncLines(sw?.final_sync || null);           // readable per-step counts
  const errorIds = finalSyncErrorIds(sw?.final_sync || null);         // ids the sync could not copy
  const buttonDisabled = !processingEnabled || running || starting;   // off in reporting-only mode or while running

  return (
    <Paper sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Request storage</Typography>
      {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}
      {status?.paused && !running && (
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

          {running ? (
            // A switch is under way: the current step, a spinner, and a disabled button.
            <Alert severity="info" icon={<CircularProgress size={18} />}>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>{STEP_LABELS[sw?.step || ''] || 'Switching to Postgres'}</Typography>
              <Typography variant="caption" color="text.secondary">
                This can take a few minutes. New requests, approvals and staff edits are refused until it finishes.
              </Typography>
            </Alert>
          ) : sw?.state === 'failed' && !confirming ? (
            // It stopped partway: nothing was switched and writes have resumed.
            // "Try again" opens the confirm step below, so this yields to it.
            <Alert severity="error">
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                The switch did not finish. Nothing was switched and writes have resumed.
              </Typography>
              {sw.error && <Typography variant="body2" sx={{ mt: 0.5 }}>{sw.error}</Typography>}
              {errorIds.length > 0 && (
                <Typography variant="body2" sx={{ mt: 0.5 }}>Items with errors: {errorIds.join(', ')}</Typography>
              )}
              <Button size="small" variant="outlined" color="inherit" sx={{ mt: 1 }} disabled={buttonDisabled} onClick={() => { setError(''); setConfirming(true); }}>
                Try again
              </Button>
            </Alert>
          ) : inPostgres ? (
            // Done: the result, with the final-sync counts when the switch carried them.
            <Alert severity="success">
              <Typography variant="body2" sx={{ fontWeight: 600 }}>Already switched. Requests now live in Postgres.</Typography>
              {syncLines.length > 0 && (
                <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2.5 }}>
                  {syncLines.map((line) => <li key={line}><Typography variant="body2">{line}</Typography></li>)}
                </Box>
              )}
            </Alert>
          ) : confirming ? (
            <Alert severity="warning">
              <Typography variant="body2" sx={{ mb: 1 }}>
                This runs one final sync and moves requests, staff and holidays to Postgres. It is one-way:
                SharePoint stops being the source, and the app never reads or writes it again. New submissions, approvals and staff edits are refused for a few
                minutes while it runs.
              </Typography>
              <Stack direction="row" spacing={1}>
                <Button size="small" variant="contained" color="warning" disabled={buttonDisabled} onClick={doSwitch}>
                  {starting ? 'Starting...' : 'Yes, switch now'}
                </Button>
                <Button size="small" disabled={starting} onClick={() => setConfirming(false)}>Cancel</Button>
              </Stack>
            </Alert>
          ) : (
            <Box>
              <Button
                size="small"
                variant="outlined"
                disabled={buttonDisabled}
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
