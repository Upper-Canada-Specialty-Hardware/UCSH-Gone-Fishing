import { useEffect, useState } from 'react';
import { Alert, Box, Button, Paper, Stack, Typography } from '@mui/material';
import { copyRequestsToPostgres, getRequestStorage } from '../../api/client';

/** Readable names for the three request lists in the report. */
const LIST_LABELS: Record<string, string> = {
  leave: 'Leave',
  overtime: 'Overtime',
  carryover_payout: 'Carry-over / payout',
};

/**
 * Where the request lists live (SharePoint or Postgres), how many items each
 * side holds, and the one-time copy from SharePoint that comes before the move.
 *
 * @param props.processingEnabled - Disables the copy button in reporting-only mode.
 * @returns A small status card for the Data checks screen.
 */
export default function RequestStorageCard({ processingEnabled }: { processingEnabled: boolean }) {
  const [report, setReport] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState('');

  /** Load the storage report. */
  const load = () =>
    getRequestStorage().then((r) => setReport(r.data)).catch(() => setError('Could not check the request storage.'));

  useEffect(() => { load(); }, []);

  /** Copy every request from SharePoint, then show the new counts. */
  const copy = async () => {
    setBusy(true);
    setError('');
    setDone('');
    try {
      const res = await copyRequestsToPostgres();
      const total = Object.values(res.data.lists || {}).reduce((n: number, v: any) => n + v.source_count, 0);
      setDone(`Copied ${total} requests from SharePoint.`);
      await load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'The copy did not finish. Nothing already copied is lost; run it again.');
    } finally {
      setBusy(false);
    }
  };

  if (!report && !error) return null;
  const inPostgres = report?.storage === 'postgres';
  const leftToCopy = report
    ? Object.values(report.lists || {}).reduce((n: number, v: any) => n + (v.only_in_sharepoint || 0), 0)
    : 0;
  return (
    <Paper sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Request storage</Typography>
      {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}
      {done && <Alert severity="success" sx={{ mt: 1 }}>{done}</Alert>}
      {report && (
        <Stack spacing={1} sx={{ mt: 1 }}>
          <Typography variant="body2">
            Requests are read and written in {inPostgres ? 'Postgres' : 'SharePoint'} (STORAGE_REQUESTS).
          </Typography>
          {Object.entries(report.lists || {}).map(([key, v]: any) => (
            <Typography key={key} variant="body2" color="text.secondary">
              {LIST_LABELS[key] || key}: {v.error
                ? `SharePoint could not be read (${v.error})`
                : `${v.sharepoint_count} in SharePoint, ${v.postgres_count} in Postgres, ${v.only_in_sharepoint} not copied yet`}
            </Typography>
          ))}
          {!inPostgres && (
            <>
              <Box>
                <Button size="small" variant="outlined" disabled={!processingEnabled || busy} onClick={copy}>
                  {busy ? 'Copying...' : 'Copy requests from SharePoint'}
                </Button>
              </Box>
              {leftToCopy === 0 && (
                <Alert severity="info">
                  Every request is copied. Set STORAGE_REQUESTS to postgres right after the last copy, so nothing decided in
                  SharePoint in between is missed.
                </Alert>
              )}
            </>
          )}
          {inPostgres && leftToCopy > 0 && (
            <Alert severity="info">
              Items not copied are Microsoft Form requests waiting to be moved in; they move as they are processed.
            </Alert>
          )}
        </Stack>
      )}
    </Paper>
  );
}
