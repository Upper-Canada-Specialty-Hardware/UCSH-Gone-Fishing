import { useEffect, useMemo, useState } from 'react';
import {
  Alert, Autocomplete, Box, Button, Chip, CircularProgress,
  Dialog, DialogActions, DialogContent, DialogTitle,
  Stack, TextField, ToggleButton, ToggleButtonGroup, Typography,
} from '@mui/material';
import { DataGrid, GridColDef } from '@mui/x-data-grid';
import { getAdminEmailLog } from '../api/client';
import { SHARED_DATA_GRID_PROPS } from './dataGridDefaults';

/** One SMTP2GO call as the backend returns it (snake_case wire format). */
interface EmailLogEntry {
  id: number;
  attempted_at: string | null;
  duration_ms: number | null;
  outcome: string;
  http_status: number | null;
  succeeded: number | null;
  failed: number | null;
  smtp2go_email_id: string | null;
  smtp2go_request_id: string | null;
  sender: string;
  subject: string;
  to: string[];
  cc: string[];
  request_url: string;
  request: Record<string, unknown> | string;
  response_body: string | null;
  no_response_reason: string | null;
}

/** The admin email-log endpoint's response. */
interface EmailLogResponse {
  employee_id: string | null;
  employee_name: string | null;
  address: string | null;
  directory_lookup: 'ok' | 'not_found' | 'no_address' | 'skipped';
  days: number;
  log_since: string | null;
  count: number;
  emails: EmailLogEntry[];
}

type ChipColor = 'success' | 'warning' | 'error' | 'default';

// How to read each outcome. The `meaning` text is what an admin needs to
// decide whether the problem is in our code or past it.
const OUTCOMES: Record<string, { label: string; color: ChipColor; meaning: string }> = {
  accepted: {
    label: 'Accepted',
    color: 'success',
    meaning: 'SMTP2GO answered HTTP 200 and queued the email for every recipient. '
      + 'If it never arrived, the problem is past our code: SMTP2GO delivery or the mailbox. '
      + 'Quote the email_id to SMTP2GO or IT.',
  },
  partially_accepted: {
    label: 'Partly Accepted',
    color: 'warning',
    meaning: 'SMTP2GO queued the email for some recipients and refused others. '
      + 'The response body names the refused addresses and why.',
  },
  rejected: {
    label: 'Rejected (HTTP 200)',
    color: 'error',
    meaning: 'SMTP2GO answered HTTP 200 but queued nothing. The response body says why. '
      + 'This is our request or our data being refused, not a delivery problem.',
  },
  http_error: {
    label: 'HTTP Error',
    color: 'error',
    meaning: 'SMTP2GO refused the request itself (4xx or 5xx). The response body has the reason.',
  },
  unreadable_response: {
    label: 'Unreadable Response',
    color: 'warning',
    meaning: 'SMTP2GO answered, but not with the documented JSON. The raw body is shown below.',
  },
  no_response: {
    label: 'No Response',
    color: 'error',
    meaning: 'The request never completed (timeout, DNS, connection refused). Nothing was sent.',
  },
  not_attempted: {
    label: 'Not Attempted',
    color: 'default',
    meaning: 'SMTP2GO was never called because the recipient address was blank. '
      + 'Fix the Staff Directory record.',
  },
};

const WINDOW_OPTIONS = [7, 30, 90];

/**
 * Render an ISO instant in Toronto time, the timezone HR works in.
 * @param iso - ISO-8601 instant from the backend, or null.
 * @returns A short local date-time, or '' for null.
 */
function formatToronto(iso: string | null): string {
  if (!iso) return '';
  return new Date(iso).toLocaleString('en-CA', {
    timeZone: 'America/Toronto',
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

/**
 * Pretty-print JSON text for the detail dialog; fall back to the raw text.
 * @param value - JSON text, an already-parsed object, or null.
 * @returns Indented JSON, or the input as a string when it is not JSON.
 */
function pretty(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value !== 'string') return JSON.stringify(value, null, 2);
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

interface Props {
  employees: any[];
}

/**
 * Admin tab: every SMTP2GO request/response that named one employee.
 *
 * Pick an employee, choose a window, and the table lists each call the
 * backend made to SMTP2GO with that person in To or CC: when, the HTTP
 * status, what SMTP2GO said (accepted / rejected counts, email_id), and a
 * detail dialog with the redacted request and the response body verbatim.
 * An empty result is spelled out, because "no row" is itself the finding:
 * the backend never asked SMTP2GO to email this address in that window.
 *
 * @param employees - Staff Directory rows from the admin balances call
 *   (id, name, department, email), used only for the picker.
 */
export default function EmailLog({ employees }: Props) {
  const [selected, setSelected] = useState<any | null>(null);
  const [days, setDays] = useState<number>(30);
  const [data, setData] = useState<EmailLogResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<EmailLogEntry | null>(null);

  // Fetch whenever the person or the window changes.
  useEffect(() => {
    if (!selected) {
      setData(null);
      return;
    }
    let cancelled = false;                                       // ignore a stale answer after a re-pick
    setLoading(true);
    setError(null);
    getAdminEmailLog({ employee_id: String(selected.id), days })
      .then((res) => { if (!cancelled) setData(res.data); })
      .catch((e) => {
        if (!cancelled) setError(e?.response?.data?.detail || e?.message || 'Lookup failed');
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [selected, days]);

  const rows = useMemo(
    () => (data?.emails ?? []).map((e) => ({
      ...e,
      attempted_local: formatToronto(e.attempted_at),
      to_text: e.to.join(', '),
      cc_text: e.cc.join(', '),
      // "1 ok / 0 failed" reads faster than two columns of numbers.
      result_text: e.succeeded === null && e.failed === null
        ? ''
        : `${e.succeeded ?? 0} ok / ${e.failed ?? 0} failed`,
    })),
    [data],
  );

  const columns = useMemo<GridColDef[]>(() => [
    { field: 'attempted_local', headerName: 'Attempted (Toronto)', width: 190 },
    { field: 'subject', headerName: 'Subject', width: 260 },
    { field: 'to_text', headerName: 'To', width: 220 },
    { field: 'cc_text', headerName: 'CC', width: 180 },
    { field: 'http_status', headerName: 'HTTP', width: 80 },
    { field: 'result_text', headerName: 'SMTP2GO Result', width: 140 },
    {
      field: 'outcome',
      headerName: 'Outcome',
      width: 170,
      renderCell: (params) => {
        const spec = OUTCOMES[params.value as string];
        return (
          <Chip size="small" label={spec?.label ?? params.value} color={spec?.color ?? 'default'} />
        );
      },
    },
    { field: 'smtp2go_email_id', headerName: 'SMTP2GO email_id', width: 220 },
    {
      field: 'actions',
      headerName: '',
      width: 100,
      sortable: false,
      filterable: false,
      renderCell: (params) => (
        <Button size="small" onClick={() => setDetail(params.row as EmailLogEntry)}>
          Details
        </Button>
      ),
    },
  ], []);

  // Window start, and whether the log is younger than the window (so an
  // empty table must not be read as "nothing was sent for 30 days").
  const windowStart = useMemo(() => new Date(Date.now() - days * 24 * 3600 * 1000), [days]);
  const logSince = data?.log_since ? new Date(data.log_since) : null;
  const logYoungerThanWindow = logSince !== null && logSince > windowStart;

  /**
   * The one-line reading of the whole result for the selected person.
   * @returns An Alert, or null while nothing has been looked up.
   */
  function renderVerdict() {
    if (!data) return null;
    const name = data.employee_name || selected?.name || `employee ${data.employee_id}`;
    if (data.directory_lookup === 'not_found') {
      return (
        <Alert severity="warning">
          The Staff Directory has no employee with id {data.employee_id} (or SharePoint was
          unreachable). Only an address can be searched, so nothing was looked up.
        </Alert>
      );
    }
    if (data.directory_lookup === 'no_address') {
      return (
        <Alert severity="error">
          {name} has no email address in the Staff Directory, so the backend could never have
          emailed them. Fix the directory record first.
        </Alert>
      );
    }
    if (data.count === 0) {
      return (
        <Alert severity="info">
          No SMTP2GO call named {data.address} since {formatToronto(windowStart.toISOString())}.
          The backend never attempted an email to this address in that window.
          {logYoungerThanWindow && (
            <> Note: the log only covers sends since {formatToronto(data.log_since)}; earlier
            sends left no record.</>
          )}
        </Alert>
      );
    }
    return (
      <Alert severity="success">
        {data.count} SMTP2GO call{data.count === 1 ? '' : 's'} named {data.address} in the last{' '}
        {data.days} days. Open Details on a row to see the request and SMTP2GO's answer.
        {logYoungerThanWindow && (
          <> The log covers sends since {formatToronto(data.log_since)}.</>
        )}
      </Alert>
    );
  }

  const detailSpec = detail ? OUTCOMES[detail.outcome] : null;

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Every email the backend asks SMTP2GO to send is recorded here with SMTP2GO's answer.
        An Accepted row that never reached the inbox is a delivery or mailbox problem, not a code
        problem. A missing row means the backend never made the request.
      </Typography>

      <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ mb: 2 }} alignItems="center">
        <Autocomplete
          sx={{ flex: 1, minWidth: 300 }}
          options={employees}
          getOptionLabel={(opt: any) => `${opt.name} (${opt.department})`}
          value={selected}
          onChange={(_, val) => setSelected(val)}
          renderInput={(params) => <TextField {...params} label="Select Employee" />}
        />
        <ToggleButtonGroup
          exclusive
          size="small"
          value={days}
          onChange={(_, v) => { if (v) setDays(v); }}
        >
          {WINDOW_OPTIONS.map((d) => (
            <ToggleButton key={d} value={d}>Last {d} days</ToggleButton>
          ))}
        </ToggleButtonGroup>
        {loading && <CircularProgress size={24} />}
      </Stack>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {data && <Box sx={{ mb: 2 }}>{renderVerdict()}</Box>}

      {data && data.count > 0 && (
        <DataGrid
          rows={rows}
          columns={columns}
          {...SHARED_DATA_GRID_PROPS}
          initialState={{
            pagination: { paginationModel: { pageSize: 25 } },
          }}
        />
      )}

      <Dialog open={detail !== null} onClose={() => setDetail(null)} maxWidth="md" fullWidth>
        <DialogTitle>SMTP2GO exchange</DialogTitle>
        <DialogContent dividers>
          {detail && (
            <Stack spacing={2}>
              <Box>
                <Chip
                  size="small"
                  label={detailSpec?.label ?? detail.outcome}
                  color={detailSpec?.color ?? 'default'}
                  sx={{ mr: 1 }}
                />
                <Typography component="span" variant="body2">{detailSpec?.meaning}</Typography>
              </Box>
              <Typography variant="body2">
                Attempted {formatToronto(detail.attempted_at)}
                {detail.duration_ms !== null && ` (answered in ${detail.duration_ms} ms)`}
                {detail.http_status !== null && `, HTTP ${detail.http_status}`}
                {detail.smtp2go_email_id && `, email_id ${detail.smtp2go_email_id}`}
                {detail.smtp2go_request_id && `, request_id ${detail.smtp2go_request_id}`}
              </Typography>
              {detail.no_response_reason && (
                <Alert severity="warning">{detail.no_response_reason}</Alert>
              )}
              <Box>
                <Typography variant="subtitle2">Request (POST {detail.request_url})</Typography>
                <Typography variant="caption" color="text.secondary">
                  The API key and the HTML body are never stored; the body is represented by its
                  size and SHA-256.
                </Typography>
                <Box component="pre" sx={{ m: 0, mt: 1, p: 1.5, bgcolor: 'grey.100', overflowX: 'auto', fontSize: 12 }}>
                  {pretty(detail.request)}
                </Box>
              </Box>
              <Box>
                <Typography variant="subtitle2">Response body (verbatim)</Typography>
                <Box component="pre" sx={{ m: 0, mt: 1, p: 1.5, bgcolor: 'grey.100', overflowX: 'auto', fontSize: 12 }}>
                  {detail.response_body ? pretty(detail.response_body) : '(no response body)'}
                </Box>
              </Box>
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDetail(null)}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
