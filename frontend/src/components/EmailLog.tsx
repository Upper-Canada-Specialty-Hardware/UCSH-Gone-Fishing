import { ReactNode, useEffect, useMemo, useState } from 'react';
import {
  Alert, Autocomplete, Box, Button, Chip, CircularProgress,
  Dialog, DialogActions, DialogContent, DialogTitle, Paper,
  Stack, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, ToggleButton, ToggleButtonGroup, Typography, useMediaQuery,
} from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { getAdminEmailLog, getAdminEmailLogRecent, sendAdminTestEmail } from '../api/client';

/**
 * One send as the backend returns it (snake_case wire format). A row can come
 * from either service; `service` says which, and `message_id` is that service's
 * own id (the old `smtp2go_email_id` is kept for callers that still read it).
 */
interface EmailLogEntry {
  id: number;
  attempted_at: string | null;
  duration_ms: number | null;
  outcome: string;
  http_status: number | null;
  succeeded: number | null;
  failed: number | null;
  service: 'smtp2go' | 'ucsh_mailer' | 'unknown';
  message_id: string | null;
  smtp2go_email_id: string | null;
  smtp2go_request_id: string | null;
  sender: string;
  subject: string;
  to: string[];
  cc: string[];
  request_url: string;
  likely_duplicate: boolean;
  request: Record<string, unknown> | string;
  response_body: string | null;
  no_response_reason: string | null;
}

/** The per-person email-log endpoint's response. */
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

/** The recent (any-service, no person) endpoint's response. */
interface RecentResponse {
  hours: number;
  since: string;
  log_since: string | null;
  limit: number;
  count: number;
  truncated: boolean;
  emails: EmailLogEntry[];
}

type ChipColor = 'success' | 'warning' | 'error' | 'default';

// How each service names itself in prose, so one wording covers both paths.
const SERVICE_NAME: Record<string, string> = {
  smtp2go: 'SMTP2GO',
  ucsh_mailer: 'the UCSH mailer (Microsoft HVE)',
  unknown: 'the email service',
};

// Short label and colour for each outcome; the prose is built per service below.
const OUTCOME_SPEC: Record<string, { label: string; color: ChipColor }> = {
  accepted: { label: 'Accepted', color: 'success' },
  partially_accepted: { label: 'Partly accepted', color: 'warning' },
  rejected: { label: 'Rejected (HTTP 200)', color: 'error' },
  http_error: { label: 'HTTP error', color: 'error' },
  unreadable_response: { label: 'Unreadable response', color: 'warning' },
  no_response: { label: 'No response', color: 'error' },
  not_attempted: { label: 'Not attempted', color: 'default' },
};

// Windows offered in each mode. Recent is in hours so "last 24 hours" is exact;
// per-person stays in days, matching the link lifetime and the first reminder.
const RECENT_WINDOWS = [
  { hours: 24, label: 'Last 24 hours' },
  { hours: 168, label: 'Last 7 days' },
  { hours: 720, label: 'Last 30 days' },
];
const PERSON_WINDOWS = [7, 30, 90];

/**
 * The outcome chip's label and colour for a row.
 * @param outcome - The backend outcome value.
 * @returns Label and MUI colour; a sensible default for an unknown value.
 */
function outcomeChip(outcome: string): { label: string; color: ChipColor } {
  return OUTCOME_SPEC[outcome] ?? { label: outcome, color: 'default' };
}

/**
 * Plain-language reading of an outcome for one service, for the detail dialog.
 * @param outcome - The backend outcome value.
 * @param service - Which service produced the row.
 * @returns A sentence saying what the outcome means and what to do next.
 */
function outcomeMeaning(outcome: string, service: string): string {
  const name = SERVICE_NAME[service] ?? SERVICE_NAME.unknown;
  switch (outcome) {
    case 'accepted':
      return `${name} accepted the request and queued the email for every recipient`
        + (service === 'ucsh_mailer'
          ? ' (status "sent", or "logged" when the mailer runs with sending switched off). '
          : '. ')
        + 'If it never arrived, the problem is past our code: delivery or the mailbox. '
        + 'Quote the message id to IT.';
    case 'partially_accepted':
      return `${name} accepted some recipients and refused others. `
        + 'The response body names the refused addresses and why.';
    case 'rejected':
      return `${name} answered HTTP 200 but queued nothing. The response body says why. `
        + 'This is our request or our data being refused, not a delivery problem.';
    case 'http_error':
      return `${name} refused the request itself (4xx or 5xx). The response body has the reason`
        + (service === 'ucsh_mailer'
          ? ', such as a recipient outside the allowed internal domains, a reused idempotency '
            + 'key, or HVE being unavailable.'
          : '.');
    case 'unreadable_response':
      return `${name} answered, but not with the shape we expect. The raw body is shown below.`;
    case 'no_response':
      return `The request to ${name} never completed (timeout, DNS, connection refused). `
        + 'Nothing was sent.';
    case 'not_attempted':
      return `${name} was never called because the recipient address was blank. `
        + 'Fix the Staff Directory record.';
    default:
      return '';
  }
}

/**
 * The service chip's label and colour for a row.
 * @param service - Which service produced the row.
 * @returns Label and MUI colour.
 */
function serviceChip(service: string): { label: string; color: ChipColor } {
  if (service === 'smtp2go') return { label: 'SMTP2GO', color: 'default' };
  if (service === 'ucsh_mailer') return { label: 'UCSH mailer', color: 'success' };
  return { label: 'Unknown', color: 'warning' };
}

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

type Mode = 'recent' | 'person';

/**
 * Admin tab: the email-send log, for both SMTP2GO and the UCSH mailer.
 *
 * Two modes share one table and one detail dialog. "All recent" needs no person
 * and lists every email in a window, newest first, flagging a subject that
 * reached the same recipient twice within five minutes (a likely double-send).
 * "By employee" is the original per-person lookup with its test-send button. No
 * table scrolls sideways: cells wrap, lower-priority columns drop with the
 * footer naming them (every value is still in the row's detail), and below about
 * 560px each row becomes a labelled card.
 *
 * @param employees - Staff Directory rows (id, name, department, email) for the
 *   person picker in "By employee" mode.
 * @returns The Email log admin tab.
 */
export default function EmailLog({ employees }: Props) {
  const theme = useTheme();
  // Layout tiers. Below 560px the table becomes cards; the two optional columns
  // appear only when there is room, and the footer names any that are hidden.
  const asCards = !useMediaQuery('(min-width:560px)');
  const showService = useMediaQuery(theme.breakpoints.up('md'));   // Service column from ~900px
  const showStatus = useMediaQuery(theme.breakpoints.up('lg'));    // HTTP column from ~1200px

  const [mode, setMode] = useState<Mode>('recent');

  // Per-person mode state.
  const [selected, setSelected] = useState<any | null>(null);
  const [days, setDays] = useState<number>(30);
  const [personData, setPersonData] = useState<EmailLogResponse | null>(null);
  const [sending, setSending] = useState(false);

  // Recent mode state.
  const [hours, setHours] = useState<number>(24);
  const [recentData, setRecentData] = useState<RecentResponse | null>(null);
  const [filter, setFilter] = useState('');                        // recipient/subject text, client-side

  // Shared state.
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<EmailLogEntry | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);               // bumped after a test send

  // Per-person fetch: whenever the person or the window changes, or a test lands.
  useEffect(() => {
    if (mode !== 'person') return;
    if (!selected) { setPersonData(null); return; }
    let cancelled = false;                                         // ignore a stale answer after a re-pick
    setLoading(true);
    setError(null);
    getAdminEmailLog({ employee_id: String(selected.id), days })
      .then((res) => { if (!cancelled) setPersonData(res.data); })
      .catch((e) => {
        if (!cancelled) setError(e?.response?.data?.detail || e?.message || 'Lookup failed');
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [mode, selected, days, refreshTick]);

  // Recent fetch: whenever the window changes, the mode is entered, or a test
  // lands. The text filter is applied client-side over the returned window, so
  // typing does not re-query.
  useEffect(() => {
    if (mode !== 'recent') return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getAdminEmailLogRecent({ hours })
      .then((res) => { if (!cancelled) setRecentData(res.data); })
      .catch((e) => {
        if (!cancelled) setError(e?.response?.data?.detail || e?.message || 'Lookup failed');
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [mode, hours, refreshTick]);

  /**
   * Send the labelled test email to the selected person, then show its row.
   * Goes through the same send path as every system email; touches no request,
   * no manager, no SharePoint item.
   */
  const handleSendTest = async () => {
    if (!selected) return;
    setSending(true);
    setError(null);
    try {
      const res = await sendAdminTestEmail({ employee_id: String(selected.id) });
      setRefreshTick((t) => t + 1);                               // reload the table with the new row
      if (res.data.email) setDetail(res.data.email);              // open the service's answer at once
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Test send failed');
    } finally {
      setSending(false);
    }
  };

  // The rows on screen: the active mode's rows, filtered client-side in recent
  // mode. The duplicate flag is set by the backend over the whole window, so a
  // client-side filter never changes it.
  const rows = useMemo<EmailLogEntry[]>(() => {
    const all = mode === 'recent' ? recentData?.emails ?? [] : personData?.emails ?? [];
    if (mode !== 'recent') return all;
    const q = filter.trim().toLowerCase();
    if (!q) return all;
    return all.filter((e) => {
      const text = `${e.subject} ${e.to.join(' ')} ${e.cc.join(' ')}`.toLowerCase();
      return text.includes(q);
    });
  }, [mode, recentData, personData, filter]);

  // Which optional columns are hidden right now, for the footer note.
  const hiddenColumns = useMemo(() => {
    const names: string[] = [];
    if (!showService) names.push('Service');
    if (!showStatus) names.push('HTTP status');
    return names;
  }, [showService, showStatus]);

  const dupCount = useMemo(() => rows.filter((e) => e.likely_duplicate).length, [rows]);

  /**
   * One small chip saying a row is a likely duplicate send.
   * @returns The chip, or null when the row is not flagged.
   */
  function dupChip(entry: EmailLogEntry) {
    if (!entry.likely_duplicate) return null;
    return (
      <Chip
        size="small"
        color="warning"
        variant="outlined"
        label="Possible duplicate"
        sx={{ ml: { sm: 1 }, mt: { xs: 0.5, sm: 0 } }}
      />
    );
  }

  /**
   * One labelled field inside a mobile card.
   * @param label - The field name.
   * @param children - The field value.
   * @returns A label/value row.
   */
  function CardField({ label, children }: { label: string; children: ReactNode }) {
    return (
      <Box sx={{ display: 'flex', gap: 1, alignItems: 'baseline' }}>
        <Typography variant="caption" color="text.secondary" sx={{ minWidth: 76, flexShrink: 0 }}>
          {label}
        </Typography>
        <Box sx={{ minWidth: 0, wordBreak: 'break-word' }}>{children}</Box>
      </Box>
    );
  }

  /**
   * The rows as a wide table (>=560px). Lower-priority columns drop by
   * breakpoint; the whole row opens the detail dialog.
   * @returns The table element.
   */
  function renderTable() {
    return (
      <Table size="small" sx={{ '& td, & th': { borderColor: 'divider', verticalAlign: 'top' } }}>
        <TableHead>
          <TableRow>
            <TableCell>Attempted (Toronto)</TableCell>
            <TableCell>Recipient</TableCell>
            <TableCell>Subject</TableCell>
            {showService && <TableCell>Service</TableCell>}
            <TableCell>Outcome</TableCell>
            {showStatus && <TableCell>HTTP</TableCell>}
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((e) => {
            const oc = outcomeChip(e.outcome);
            const sc = serviceChip(e.service);
            return (
              <TableRow
                key={e.id}
                hover
                tabIndex={0}
                onClick={() => setDetail(e)}
                onKeyDown={(ev) => { if (ev.key === 'Enter') setDetail(e); }}
                sx={{ cursor: 'pointer' }}
              >
                <TableCell sx={{ whiteSpace: { md: 'nowrap' } }}>{formatToronto(e.attempted_at)}</TableCell>
                <TableCell sx={{ wordBreak: 'break-word', maxWidth: 260 }}>
                  {e.to.join(', ')}
                  {e.cc.length > 0 && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                      cc: {e.cc.join(', ')}
                    </Typography>
                  )}
                </TableCell>
                <TableCell sx={{ wordBreak: 'break-word', maxWidth: 320 }}>
                  {e.subject}
                  {dupChip(e)}
                </TableCell>
                {showService && (
                  <TableCell><Chip size="small" label={sc.label} color={sc.color} /></TableCell>
                )}
                <TableCell><Chip size="small" label={oc.label} color={oc.color} /></TableCell>
                {showStatus && <TableCell>{e.http_status ?? ''}</TableCell>}
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    );
  }

  /**
   * The rows as labelled cards (below 560px), so nothing scrolls sideways.
   * @returns A stack of cards.
   */
  function renderCards() {
    return (
      <Stack spacing={1.5}>
        {rows.map((e) => {
          const oc = outcomeChip(e.outcome);
          const sc = serviceChip(e.service);
          return (
            <Paper
              key={e.id}
              variant="outlined"
              onClick={() => setDetail(e)}
              sx={{ p: 1.5, cursor: 'pointer' }}
            >
              <Stack spacing={0.5}>
                <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap', alignItems: 'center' }}>
                  <Chip size="small" label={oc.label} color={oc.color} />
                  <Chip size="small" label={sc.label} color={sc.color} />
                  {dupChip(e)}
                </Box>
                <CardField label="Subject"><Typography variant="body2">{e.subject}</Typography></CardField>
                <CardField label="To"><Typography variant="body2">{e.to.join(', ')}</Typography></CardField>
                {e.cc.length > 0 && (
                  <CardField label="CC"><Typography variant="body2">{e.cc.join(', ')}</Typography></CardField>
                )}
                <CardField label="When">
                  <Typography variant="body2">{formatToronto(e.attempted_at)}</Typography>
                </CardField>
              </Stack>
            </Paper>
          );
        })}
      </Stack>
    );
  }

  /**
   * The one-line reading of a per-person lookup.
   * @returns An Alert, or null while nothing has been looked up.
   */
  function renderPersonVerdict() {
    const data = personData;
    if (!data) return null;
    const name = data.employee_name || selected?.name || `employee ${data.employee_id}`;
    const windowStart = new Date(Date.now() - days * 24 * 3600 * 1000);
    const logSince = data.log_since ? new Date(data.log_since) : null;
    const logYoungerThanWindow = logSince !== null && logSince > windowStart;
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
    if (data.count === 0 && logSince === null) {
      return (
        <Alert severity="info">
          The log has not recorded any send yet. It only records sends made after it went live, so
          anything sent to {data.address} before that has no row here.
        </Alert>
      );
    }
    if (data.count === 0) {
      return (
        <Alert severity="info">
          No email has named {data.address} since {formatToronto(windowStart.toISOString())} (Toronto
          time). The backend never attempted an email to this address in that window.
          {logYoungerThanWindow && (
            <> Note: the log only covers sends since {formatToronto(data.log_since)} (Toronto time).</>
          )}
        </Alert>
      );
    }
    return (
      <Alert severity="success">
        {data.count} email{data.count === 1 ? '' : 's'} named {data.address} in the last {data.days}{' '}
        days. Open a row to see the request and the service's answer.
        {logYoungerThanWindow && (
          <> The log covers sends since {formatToronto(data.log_since)}.</>
        )}
      </Alert>
    );
  }

  /**
   * The one-line reading of the recent window.
   * @returns An Alert, or null before anything is loaded.
   */
  function renderRecentVerdict() {
    const data = recentData;
    if (!data) return null;
    if (data.count === 0) {
      return (
        <Alert severity="info">
          No email was sent through any service in this window.
          {data.log_since && <> The log covers sends since {formatToronto(data.log_since)} (Toronto time).</>}
        </Alert>
      );
    }
    return (
      <Alert severity={dupCount > 0 ? 'warning' : 'success'}>
        {data.count} email{data.count === 1 ? '' : 's'} in this window
        {data.truncated && ` (capped at ${data.limit}; older ones in the window are not shown)`}.
        {dupCount > 0
          ? ` ${dupCount} row${dupCount === 1 ? '' : 's'} look like a duplicate (same subject to the `
            + 'same recipient within five minutes).'
          : ' No duplicate sends detected.'}
      </Alert>
    );
  }

  const detailService = detail ? SERVICE_NAME[detail.service] ?? SERVICE_NAME.unknown : '';
  const detailOutcome = detail ? outcomeChip(detail.outcome) : null;

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Every email the backend sends is recorded here with the service's answer. Internal
        (@ucsh.com) recipients go through the UCSH mailer; everyone else goes through SMTP2GO. An
        Accepted row that never reached the inbox is a delivery or mailbox problem, not a code
        problem. A missing row means the backend never made the request.
      </Typography>

      <ToggleButtonGroup
        exclusive
        size="small"
        value={mode}
        onChange={(_, v) => { if (v) { setMode(v); setError(null); } }}
        sx={{ mb: 2 }}
      >
        <ToggleButton value="recent">All recent</ToggleButton>
        <ToggleButton value="person">By employee</ToggleButton>
      </ToggleButtonGroup>

      {mode === 'recent' && (
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ mb: 2 }} alignItems={{ md: 'center' }}>
          <ToggleButtonGroup
            exclusive
            size="small"
            value={hours}
            onChange={(_, v) => { if (v) setHours(v); }}
          >
            {RECENT_WINDOWS.map((w) => (
              <ToggleButton key={w.hours} value={w.hours}>{w.label}</ToggleButton>
            ))}
          </ToggleButtonGroup>
          <TextField
            size="small"
            label="Filter by recipient or subject"
            value={filter}
            onChange={(ev) => setFilter(ev.target.value)}
            sx={{ flex: 1, minWidth: 240 }}
          />
          {loading && <CircularProgress size={24} />}
        </Stack>
      )}

      {mode === 'person' && (
        <>
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ mb: 2 }} alignItems={{ md: 'center' }}>
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
              {PERSON_WINDOWS.map((d) => (
                <ToggleButton key={d} value={d}>Last {d} days</ToggleButton>
              ))}
            </ToggleButtonGroup>
            <Button
              variant="outlined"
              disabled={!personData?.address || loading || sending}
              onClick={handleSendTest}
            >
              {sending ? 'Sending...' : 'Send test email'}
            </Button>
            {loading && <CircularProgress size={24} />}
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
            Send test email delivers a short message titled "Leave system email test" to the selected
            person's directory address through the normal send path, then opens the service's answer.
            It creates no request and notifies no manager.
          </Typography>
        </>
      )}

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      <Box sx={{ mb: 2 }}>{mode === 'recent' ? renderRecentVerdict() : renderPersonVerdict()}</Box>

      {rows.length > 0 && (
        <Box>
          {asCards ? renderCards() : renderTable()}
          {!asCards && hiddenColumns.length > 0 && (
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
              Hidden on this screen: {hiddenColumns.join(', ')}. Open a row for every value.
            </Typography>
          )}
        </Box>
      )}
      {mode === 'recent' && filter.trim() && rows.length === 0 && recentData && recentData.count > 0 && (
        <Typography color="text.secondary" sx={{ p: 2 }}>
          Nothing in this window matches "{filter.trim()}". Clear the filter to see all{' '}
          {recentData.count} rows.
        </Typography>
      )}

      <Dialog open={detail !== null} onClose={() => setDetail(null)} maxWidth="md" fullWidth>
        <DialogTitle>Email exchange</DialogTitle>
        <DialogContent dividers>
          {detail && (
            <Stack spacing={2}>
              <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
                <Chip size="small" label={detailOutcome?.label ?? detail.outcome} color={detailOutcome?.color ?? 'default'} />
                <Chip size="small" label={serviceChip(detail.service).label} color={serviceChip(detail.service).color} />
                {detail.likely_duplicate && (
                  <Chip size="small" color="warning" variant="outlined" label="Possible duplicate" />
                )}
              </Box>
              <Typography variant="body2">{outcomeMeaning(detail.outcome, detail.service)}</Typography>
              {detail.likely_duplicate && (
                <Alert severity="warning">
                  The same subject reached the same recipient again within five minutes. This is how a
                  double-send shows up: check whether the request fired the email twice.
                </Alert>
              )}
              <Typography variant="body2">
                Sent through {detailService}, from {detail.sender || '(no sender recorded)'}.
                {' '}Attempted {formatToronto(detail.attempted_at)}
                {detail.duration_ms !== null && ` (answered in ${detail.duration_ms} ms)`}
                {detail.http_status !== null && `, HTTP ${detail.http_status}`}
                {detail.message_id && `, message id ${detail.message_id}`}
                {detail.smtp2go_request_id && `, request id ${detail.smtp2go_request_id}`}
              </Typography>
              {detail.no_response_reason && (
                <Alert severity="warning">{detail.no_response_reason}</Alert>
              )}
              <Box>
                <Typography variant="subtitle2">Request (POST {detail.request_url})</Typography>
                <Typography variant="caption" color="text.secondary">
                  The API key and the HTML body are never stored; the body is represented by its size
                  and SHA-256.
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
