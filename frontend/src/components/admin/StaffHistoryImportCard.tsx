import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, CircularProgress, Paper, Stack, Typography } from '@mui/material';
import { getStaffHistoryImport, startStaffHistoryImport } from '../../api/client';

/**
 * The staff version-history backfill's progress, as GET /admin/staff-history/import
 * returns it (#448).
 */
interface ImportProgress {
  /** A run is in flight right now. */
  running: boolean;
  /** When this run started / finished, ISO 8601, or null. */
  started_at: string | null;
  finished_at: string | null;
  /** People to process this run, and how many are done. */
  people_total: number;
  people_done: number;
  /** History rows written so far. */
  rows_written: number;
  /** Per-person failures: the id and a short reason. */
  errors: { employee_id: string; error: string }[];
  /** "done" / "failed" once a run ends, else null. */
  result: string | null;
  /** A run-wide error message when it failed. */
  error: string | null;
}

/**
 * A small admin tool to backfill each person's Staff Directory version history
 * into the app (#448). It sits beside the storage tools on the Data checks
 * screen. One button starts the one-off import in the background; while it runs
 * the card polls every two seconds and shows how many people are done, how many
 * history rows were written, and any per-person read failures. Re-running is
 * safe: it replaces each person's imported rows, never the app's own edits.
 *
 * @returns The status-and-action card.
 */
export default function StaffHistoryImportCard() {
  const [progress, setProgress] = useState<ImportProgress | null>(null);   // the latest progress, or null before first load
  const [error, setError] = useState('');                                  // a readable error, or empty
  const [starting, setStarting] = useState(false);                         // the POST is in flight, before polling takes over

  /** Load the import progress; swallows a read error into the card's error line. */
  const load = useCallback(async () => {
    try {
      const r = await getStaffHistoryImport();
      setProgress(r.data as ImportProgress);
    } catch {
      setError('Could not read the import status.');
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const running = progress?.running === true;                              // a run is under way

  // While a run is in flight (started here or already running when the card
  // opened), poll every 2s so the counts and the final result follow it.
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => void load(), 2000);
    return () => clearInterval(t);
  }, [running, load]);

  /** Start the backfill. The server runs it in the background; the poll shows progress. */
  const start = async () => {
    setStarting(true);
    setError('');
    try {
      await startStaffHistoryImport();
    } catch (err: any) {
      // 409 means a run is already going.
      setError(err?.response?.data?.detail || 'The import did not start. Try again in a moment.');
    } finally {
      await load();                                                        // pick up the running state
      setStarting(false);
    }
  };

  return (
    <Paper sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Staff history import</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
        Brings each person's entitlement and balance history from SharePoint into their version
        history. Safe to run again: it replaces only the imported rows.
      </Typography>
      {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}

      {running ? (
        // A run is under way: a spinner and the live counts, button disabled.
        <Alert severity="info" icon={<CircularProgress size={18} />} sx={{ mt: 1 }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            Importing {progress?.people_done ?? 0} of {progress?.people_total ?? 0} people
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {progress?.rows_written ?? 0} history rows written so far.
          </Typography>
        </Alert>
      ) : (
        <Stack spacing={1} sx={{ mt: 1 }}>
          {progress?.result === 'done' && (
            <Alert severity={progress.errors.length ? 'warning' : 'success'}>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                Imported {progress.rows_written} history rows across {progress.people_total} people.
              </Typography>
              {progress.errors.length > 0 && (
                <Typography variant="body2" sx={{ mt: 0.5 }}>
                  {progress.errors.length} could not be read and were left for a re-run.
                </Typography>
              )}
            </Alert>
          )}
          {progress?.result === 'failed' && (
            <Alert severity="error">
              <Typography variant="body2" sx={{ fontWeight: 600 }}>The import did not finish.</Typography>
              {progress.error && <Typography variant="body2" sx={{ mt: 0.5 }}>{progress.error}</Typography>}
            </Alert>
          )}
          <Box>
            <Button size="small" variant="outlined" disabled={starting} onClick={start}>
              {starting ? 'Starting...' : 'Import history from SharePoint'}
            </Button>
          </Box>
        </Stack>
      )}
    </Paper>
  );
}
