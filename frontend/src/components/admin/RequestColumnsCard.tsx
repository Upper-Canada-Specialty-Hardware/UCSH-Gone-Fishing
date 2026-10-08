import { useEffect, useState } from 'react';
import { Alert, Box, Button, Paper, Stack, Typography } from '@mui/material';
import { addRequestColumns, getRequestColumns } from '../../api/client';

/**
 * Whether the three request lists have SubmitterEmail and RequestSource, which
 * let a request from the page reach its person without a SharePoint visit.
 * Moved here from the held requests tab, unchanged, for the Data checks screen.
 *
 * @param props.processingEnabled - Disables the add button in reporting-only mode.
 * @returns A small status card.
 */
export default function RequestColumnsCard({ processingEnabled }: { processingEnabled: boolean }) {
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
    ? Object.entries(report.lists || {}).filter(([, v]: any) => v.error).map(([k, v]: any) => `${k}: ${v.error}`)
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
