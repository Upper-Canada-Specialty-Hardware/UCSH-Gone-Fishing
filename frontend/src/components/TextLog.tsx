import { ReactNode, RefObject, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Autocomplete, Box, Button, Chip, CircularProgress,
  Dialog, DialogActions, DialogContent, DialogTitle, Paper,
  Stack, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, ToggleButton, ToggleButtonGroup, Typography,
} from '@mui/material';
import { getAdminSmsLog, getAdminSmsLogRecent, sendAdminTestSms } from '../api/client';

/**
 * One outbound text as the backend returns it (snake_case wire format). It is
 * the record of what the system asked Twilio to send and what Twilio answered,
 * plus the delivery status Twilio later reported.
 */
interface SendEntry {
  type: 'send';
  id: number;
  at: string | null;
  duration_ms: number | null;
  kind: string;
  request_kind: string | null;
  request_item_id: string | null;
  outbox_id: number | null;
  request_id: string | null;
  inbound_reply_id: number | null;
  to_number: string;
  from_number: string | null;
  body: string;
  http_status: number | null;
  twilio_sid: string | null;
  twilio_status: string | null;
  error_code: string | null;
  error_message: string | null;
  no_response_reason: string | null;
  response_body: string | null;
  delivery_status: string | null;
  delivery_error_code: string | null;
  delivery_updated_at: string | null;
  delivery_history: Array<{ status: string | null; error_code: string | null; at: string }>;
}

/** One inbound text and the branch the webhook handler took. */
interface ReplyEntry {
  type: 'reply';
  id: number;
  at: string | null;
  message_sid: string | null;
  from_number: string | null;
  to_number: string | null;
  body: string | null;
  signature_ok: boolean;
  decision: string | null;
  request_type: string | null;
  item_id: string | null;
  reason: string | null;
  sender_staff_id: string | null;
  outcome: string;
  error_text: string | null;
}

type Entry = SendEntry | ReplyEntry;

/** The per-person / per-request endpoint's response. */
interface SmsLogResponse {
  employee_id: string | null;
  employee_name: string | null;
  number: string | null;
  directory_lookup: 'ok' | 'not_found' | 'no_number' | 'skipped';
  request_kind: string | null;
  item_id: string | null;
  days: number;
  log_since: string | null;
  count: number;
  sends: SendEntry[];
  replies: ReplyEntry[];
}

/** The recent (no person) endpoint's response. */
interface RecentResponse {
  hours: number;
  since: string;
  log_since: string | null;
  limit: number;
  count: number;
  sends: SendEntry[];
  replies: ReplyEntry[];
}

type ChipColor = 'success' | 'warning' | 'error' | 'default';
type Mode = 'recent' | 'person';

// Reply outcome -> short label and colour. These mirror the OUTCOME_* constants
// the backend sets, one per branch of the SMS webhook handler.
const OUTCOME_SPEC: Record<string, { label: string; color: ChipColor }> = {
  bad_signature: { label: 'Bad signature', color: 'error' },
  processing_off: { label: 'Processing off', color: 'default' },
  paused: { label: 'Paused', color: 'default' },
  unrecognized: { label: 'Unrecognized', color: 'warning' },
  not_found: { label: 'Request not found', color: 'warning' },
  already_decided: { label: 'Already decided', color: 'default' },
  not_registered: { label: 'Number not registered', color: 'warning' },
  no_access: { label: 'No access', color: 'warning' },
  approved: { label: 'Approved', color: 'success' },
  rejected: { label: 'Rejected', color: 'success' },
  refused: { label: 'Refused', color: 'error' },
  error: { label: 'Error', color: 'error' },
};

// Windows offered in each mode (hours for recent so "last 24 hours" is exact,
// days per-person matching the email log).
const RECENT_WINDOWS = [
  { hours: 24, label: 'Last 24 hours' },
  { hours: 168, label: 'Last 7 days' },
  { hours: 720, label: 'Last 30 days' },
];
const PERSON_WINDOWS = [7, 30, 90];

// Container-width thresholds (px of the Paper's own content box, not the
// viewport, because the admin sidebar eats most of a narrow window). Below
// CARD_MAX the rows become cards; the tiers add columns only when there is room.
const CARD_MAX = 560;
const WIDE_MIN = 820;    // Kind + Answer columns fit from here
const STATUS_MIN = 1040; // Delivery column fits from here

// Let a chip's text wrap instead of forcing its column wider.
const CHIP_WRAP = {
  height: 'auto',
  py: 0.25,
  '& .MuiChip-label': { whiteSpace: 'normal', wordBreak: 'break-word' },
} as const;

/**
 * Track an element's own content width with a ResizeObserver, so layout keys off
 * the space the component has rather than the viewport.
 * @returns A ref to attach, and the last measured width in px (0 until first measure).
 */
function useElementWidth<T extends HTMLElement>(): [RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.getBoundingClientRect().width);                    // first measure, before any resize
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) setWidth(entry.contentRect.width);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

/**
 * Render an ISO instant in Toronto time, the house format "Oct 26, 2026, 3:04 PM".
 * @param iso - ISO-8601 instant from the backend, or null.
 * @returns A short local date-time, or '' for null.
 */
function formatToronto(iso: string | null): string {
  if (!iso) return '';
  return new Date(iso).toLocaleString('en-US', {
    timeZone: 'America/Toronto',
    year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true,
  });
}

/**
 * The delivery chip for a send, from the status Twilio reported in a callback.
 * @param entry - The send row.
 * @returns Label and colour, or null when no callback has landed yet.
 */
function deliveryChip(entry: SendEntry): { label: string; color: ChipColor } | null {
  const status = (entry.delivery_status || '').toLowerCase();
  if (!status) return null;                                        // no callback yet
  const nice = status.charAt(0).toUpperCase() + status.slice(1);
  if (status === 'delivered') return { label: 'Delivered', color: 'success' };
  if (status === 'undelivered' || status === 'failed') {
    const code = entry.delivery_error_code ? ` (${entry.delivery_error_code})` : '';
    return { label: `${nice}${code}`, color: 'error' };
  }
  return { label: nice, color: 'default' };                        // queued / sent / sending -> neutral
}

/**
 * The chip summarising Twilio's answer to the send itself (not delivery).
 * @param entry - The send row.
 * @returns Label and colour.
 */
function sendAnswerChip(entry: SendEntry): { label: string; color: ChipColor } {
  if (entry.http_status !== null && entry.http_status >= 400) {
    const code = entry.error_code ? ` (${entry.error_code})` : '';
    return { label: `Refused${code}`, color: 'error' };            // Twilio refused the send
  }
  if (entry.http_status !== null) return { label: 'Accepted', color: 'success' };
  if (entry.no_response_reason && entry.no_response_reason.startsWith('rehearsal')) {
    return { label: 'Skipped (rehearsal)', color: 'default' };
  }
  return { label: 'Not sent', color: 'warning' };                  // bad number or network error
}

/**
 * The request a row was about, as a short label.
 * @param kind - leave / overtime / carryover-payout, or null.
 * @param itemId - The request item id, or null.
 * @returns "leave #123", or '' when not tied to a request.
 */
function requestLabel(kind: string | null, itemId: string | null): string {
  if (!kind && !itemId) return '';
  return `${kind ?? '?'}${itemId ? ` #${itemId}` : ''}`;
}

/**
 * One labelled field inside a mobile card.
 * @param props.label - The field name.
 * @param props.children - The field value.
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

interface Props {
  employees: any[];
}

/**
 * Admin tab: the text-send and text-reply log (Twilio).
 *
 * Two modes share one timeline and one detail dialog. "All recent" needs no
 * person and lists every text, sent and received, in a window, newest first.
 * "By employee" resolves the person's cell from the Staff Directory and lists
 * every text to or from that number, with a test-send button. Sends and replies
 * are interleaved by time. No table scrolls sideways: the layout keys off the
 * component's own measured width (so the admin sidebar is accounted for), cells
 * and chips wrap, lower-priority columns drop on narrow screens, and when the
 * container is narrow each row becomes a labelled, keyboard-openable card.
 *
 * @param employees - Staff Directory rows for the person picker.
 * @returns The Text log admin tab.
 */
export default function TextLog({ employees }: Props) {
  const [rootRef, width] = useElementWidth<HTMLDivElement>();
  const asCards = width > 0 && width < CARD_MAX;                   // narrow: cards, never a sideways scroll
  const showWide = width >= WIDE_MIN;                              // room for Kind + Answer
  const showStatus = width >= STATUS_MIN;                          // room for Delivery too

  const [mode, setMode] = useState<Mode>('recent');

  // Per-person mode state.
  const [selected, setSelected] = useState<any | null>(null);
  const [days, setDays] = useState<number>(30);
  const [personData, setPersonData] = useState<SmsLogResponse | null>(null);
  const [sending, setSending] = useState(false);

  // Recent mode state.
  const [hours, setHours] = useState<number>(24);
  const [recentData, setRecentData] = useState<RecentResponse | null>(null);

  // Shared state.
  const [filter, setFilter] = useState('');
  const [debouncedText, setDebouncedText] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<Entry | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);               // bumped after a test send

  // Debounce the filter so recent mode queries the server at most every 300ms.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedText(filter.trim()), 300);
    return () => clearTimeout(timer);
  }, [filter]);

  // Per-person fetch: whenever the person or the window changes, or a test lands.
  useEffect(() => {
    if (mode !== 'person') return;
    if (!selected) {
      setPersonData(null);
      setLoading(false);
      return;
    }
    let cancelled = false;                                         // ignore a stale answer after a re-pick
    setLoading(true);
    setError(null);
    getAdminSmsLog({ employee_id: String(selected.id), days })
      .then((res) => { if (!cancelled) setPersonData(res.data); })
      .catch((e) => {
        if (!cancelled) {
          setPersonData(null);
          setError(e?.response?.data?.detail || e?.message || 'Lookup failed');
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [mode, selected, days, refreshTick]);

  // Recent fetch: whenever the window, the settled filter, the mode or a test changes.
  useEffect(() => {
    if (mode !== 'recent') return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getAdminSmsLogRecent({ hours, text: debouncedText || undefined })
      .then((res) => { if (!cancelled) setRecentData(res.data); })
      .catch((e) => {
        if (!cancelled) {
          setRecentData(null);
          setError(e?.response?.data?.detail || e?.message || 'Lookup failed');
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [mode, hours, debouncedText, refreshTick]);

  /**
   * Send the labelled test text to the selected person, then reload the list.
   * Goes through the same send path as every system text; touches no request.
   */
  const handleSendTest = async () => {
    if (!selected) return;
    setSending(true);
    setError(null);
    try {
      const res = await sendAdminTestSms({ employee_id: String(selected.id) });
      setRefreshTick((t) => t + 1);                               // reload with the new row
      if (res.data.send) setDetail(res.data.send);                // open Twilio's answer at once
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Test send failed');
    } finally {
      setSending(false);
    }
  };

  // The interleaved timeline for the active mode, newest first. Person rows are
  // also filtered client-side by the text box; recent rows are filtered server-side.
  const rows = useMemo<Entry[]>(() => {
    const data = mode === 'recent' ? recentData : personData;
    if (!data) return [];
    const merged: Entry[] = [...data.sends, ...data.replies];
    merged.sort((a, b) => (b.at ?? '').localeCompare(a.at ?? ''));  // ISO strings sort chronologically
    if (mode === 'person') {
      const q = filter.trim().toLowerCase();
      if (q) {
        return merged.filter((e) => {
          const num = e.type === 'send' ? e.to_number : (e.from_number ?? '');
          return `${e.body ?? ''} ${num}`.toLowerCase().includes(q);
        });
      }
    }
    return merged;
  }, [mode, recentData, personData, filter]);

  // Which optional columns are hidden now, for the footer note.
  const hiddenColumns = useMemo(() => {
    const names: string[] = [];
    if (!showWide) names.push('Kind', 'Answer');
    if (!showStatus) names.push('Delivery');
    return names;
  }, [showWide, showStatus]);

  /**
   * The direction chip ("Sent" / "Received") for a row.
   * @param e - The row.
   * @returns The chip.
   */
  function directionChip(e: Entry) {
    return e.type === 'send'
      ? <Chip size="small" label="Sent" color="primary" variant="outlined" sx={CHIP_WRAP} />
      : <Chip size="small" label="Received" color="secondary" variant="outlined" sx={CHIP_WRAP} />;
  }

  /**
   * The headline chip for a row: Twilio's send answer, or the reply outcome.
   * @param e - The row.
   * @returns Label and colour.
   */
  function headlineChip(e: Entry): { label: string; color: ChipColor } {
    if (e.type === 'send') return sendAnswerChip(e);
    return OUTCOME_SPEC[e.outcome] ?? { label: e.outcome, color: 'default' };
  }

  /**
   * The rows as a wide table. Lower-priority columns drop by breakpoint; the
   * whole row opens the detail dialog.
   * @returns The table element.
   */
  function renderTable() {
    return (
      <Table size="small" sx={{ '& td, & th': { borderColor: 'divider', verticalAlign: 'top' } }}>
        <TableHead>
          <TableRow>
            <TableCell>When (Toronto)</TableCell>
            <TableCell>Direction</TableCell>
            <TableCell>Number</TableCell>
            <TableCell>Text</TableCell>
            {showWide && <TableCell>Kind</TableCell>}
            <TableCell>Answer</TableCell>
            {showStatus && <TableCell>Delivery</TableCell>}
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((e) => {
            const head = headlineChip(e);
            const num = e.type === 'send' ? e.to_number : (e.from_number ?? '');
            const kind = e.type === 'send' ? e.kind : 'reply';
            const req = e.type === 'send'
              ? requestLabel(e.request_kind, e.request_item_id)
              : requestLabel(e.request_type, e.item_id);
            const delivery = e.type === 'send' ? deliveryChip(e) : null;
            return (
              <TableRow
                key={`${e.type}-${e.id}`}
                hover
                tabIndex={0}
                onClick={() => setDetail(e)}
                onKeyDown={(ev) => { if (ev.key === 'Enter') setDetail(e); }}
                sx={{ cursor: 'pointer' }}
              >
                <TableCell sx={{ overflowWrap: 'anywhere' }}>{formatToronto(e.at)}</TableCell>
                <TableCell>{directionChip(e)}</TableCell>
                <TableCell sx={{ overflowWrap: 'anywhere', maxWidth: 160 }}>{num}</TableCell>
                <TableCell sx={{ overflowWrap: 'anywhere', maxWidth: 320 }}>
                  {e.body}
                  {req && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                      {req}
                    </Typography>
                  )}
                </TableCell>
                {showWide && <TableCell sx={{ overflowWrap: 'anywhere' }}>{kind}</TableCell>}
                <TableCell><Chip size="small" label={head.label} color={head.color} sx={CHIP_WRAP} /></TableCell>
                {showStatus && (
                  <TableCell>
                    {delivery
                      ? <Chip size="small" label={delivery.label} color={delivery.color} sx={CHIP_WRAP} />
                      : (e.type === 'send' ? <Typography variant="caption" color="text.secondary">Pending</Typography> : '')}
                  </TableCell>
                )}
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    );
  }

  /**
   * The rows as labelled, keyboard-openable cards (narrow container), so nothing
   * scrolls sideways.
   * @returns A stack of cards.
   */
  function renderCards() {
    return (
      <Stack spacing={1.5}>
        {rows.map((e) => {
          const head = headlineChip(e);
          const num = e.type === 'send' ? e.to_number : (e.from_number ?? '');
          const kind = e.type === 'send' ? e.kind : 'reply';
          const req = e.type === 'send'
            ? requestLabel(e.request_kind, e.request_item_id)
            : requestLabel(e.request_type, e.item_id);
          const delivery = e.type === 'send' ? deliveryChip(e) : null;
          return (
            <Paper
              key={`${e.type}-${e.id}`}
              variant="outlined"
              role="button"
              tabIndex={0}
              onClick={() => setDetail(e)}
              onKeyDown={(ev) => {
                if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); setDetail(e); }
              }}
              sx={{ p: 1.5, cursor: 'pointer' }}
            >
              <Stack spacing={0.5}>
                <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap', alignItems: 'center' }}>
                  {directionChip(e)}
                  <Chip size="small" label={head.label} color={head.color} sx={CHIP_WRAP} />
                  {delivery && <Chip size="small" label={delivery.label} color={delivery.color} sx={CHIP_WRAP} />}
                </Box>
                <CardField label="Text"><Typography variant="body2">{e.body}</Typography></CardField>
                <CardField label="Number"><Typography variant="body2">{num}</Typography></CardField>
                <CardField label="Kind"><Typography variant="body2">{kind}</Typography></CardField>
                {req && <CardField label="Request"><Typography variant="body2">{req}</Typography></CardField>}
                <CardField label="When"><Typography variant="body2">{formatToronto(e.at)}</Typography></CardField>
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
    if (data.directory_lookup === 'not_found') {
      return (
        <Alert severity="warning">
          The Staff Directory has no employee with id {data.employee_id} (or SharePoint was
          unreachable). Only a number can be searched, so nothing was looked up.
        </Alert>
      );
    }
    if (data.directory_lookup === 'no_number') {
      return (
        <Alert severity="error">
          {name} has no cell number in the Staff Directory, so the system could never have texted
          them. Fix the directory record first.
        </Alert>
      );
    }
    if (data.count === 0) {
      return (
        <Alert severity="info">
          No text to or from {data.number} in the last {data.days} days.
          {data.log_since && <> The log covers texts since {formatToronto(data.log_since)} (Toronto time).</>}
        </Alert>
      );
    }
    return (
      <Alert severity="success">
        {rows.length} of {data.count} text{data.count === 1 ? '' : 's'} for {data.number} shown
        {filter.trim() ? ` (filtered by "${filter.trim()}")` : ''} from the last {data.days} days.
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
          {debouncedText
            ? `No text matches "${debouncedText}" in this window.`
            : 'No text was sent or received in this window.'}
          {data.log_since && <> The log covers texts since {formatToronto(data.log_since)} (Toronto time).</>}
        </Alert>
      );
    }
    return (
      <Alert severity="success">
        {data.count} text{data.count === 1 ? '' : 's'}
        {debouncedText ? ` match "${debouncedText}"` : ''} in this window
        ({data.sends.length} sent, {data.replies.length} received).
      </Alert>
    );
  }

  return (
    <Box ref={rootRef}>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Every text the system sends through Twilio is recorded here with Twilio's answer and, as
        Twilio reports it, whether the message was delivered. Every text reply the system receives is
        recorded too, with what the webhook read and what it did. This is the only place full phone
        numbers are kept; the server logs mask them.
      </Typography>

      <ToggleButtonGroup
        exclusive
        size="small"
        value={mode}
        onChange={(_, v) => { if (v) { setMode(v); setError(null); } }}
        sx={{ mb: 2, flexWrap: 'wrap' }}
      >
        <ToggleButton value="recent">All recent</ToggleButton>
        <ToggleButton value="person">By employee</ToggleButton>
      </ToggleButtonGroup>

      {mode === 'recent' && (
        <Stack
          direction={{ xs: 'column', md: 'row' }}
          spacing={2}
          sx={{ mb: 2, flexWrap: 'wrap' }}
          alignItems={{ md: 'center' }}
          useFlexGap
        >
          <ToggleButtonGroup
            exclusive
            size="small"
            value={hours}
            onChange={(_, v) => { if (v) setHours(v); }}
            sx={{ flexWrap: 'wrap' }}
          >
            {RECENT_WINDOWS.map((w) => (
              <ToggleButton key={w.hours} value={w.hours}>{w.label}</ToggleButton>
            ))}
          </ToggleButtonGroup>
          <TextField
            size="small"
            label="Filter by number or text"
            value={filter}
            onChange={(ev) => setFilter(ev.target.value)}
            sx={{ flex: 1, minWidth: { xs: 0, sm: 220 }, width: '100%' }}
          />
          {loading && <CircularProgress size={24} />}
        </Stack>
      )}

      {mode === 'person' && (
        <>
          <Stack
            direction={{ xs: 'column', md: 'row' }}
            spacing={2}
            sx={{ mb: 2, flexWrap: 'wrap' }}
            alignItems={{ md: 'center' }}
            useFlexGap
          >
            <Autocomplete
              sx={{ flex: 1, minWidth: { xs: 0, md: 280 }, width: '100%' }}
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
              sx={{ flexWrap: 'wrap' }}
            >
              {PERSON_WINDOWS.map((d) => (
                <ToggleButton key={d} value={d}>Last {d} days</ToggleButton>
              ))}
            </ToggleButtonGroup>
            <TextField
              size="small"
              label="Filter by number or text"
              value={filter}
              onChange={(ev) => setFilter(ev.target.value)}
              sx={{ flex: 1, minWidth: { xs: 0, sm: 220 }, width: '100%' }}
            />
            <Button
              variant="outlined"
              disabled={!personData?.number || loading || sending}
              onClick={handleSendTest}
            >
              {sending ? 'Sending...' : 'Send test text'}
            </Button>
            {loading && <CircularProgress size={24} />}
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
            Send test text delivers a short labelled message to the selected person's directory cell
            number through the normal send path, then opens Twilio's answer. It creates no request and
            notifies no manager.
          </Typography>
        </>
      )}

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {!error && (
        <Box sx={{ mb: 2 }}>{mode === 'recent' ? renderRecentVerdict() : renderPersonVerdict()}</Box>
      )}

      {!error && rows.length > 0 && (
        <Box>
          {asCards ? renderCards() : renderTable()}
          {!asCards && hiddenColumns.length > 0 && (
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
              Hidden on this screen: {hiddenColumns.join(', ')}. Open a row for every value.
            </Typography>
          )}
        </Box>
      )}

      <Dialog open={detail !== null} onClose={() => setDetail(null)} maxWidth="md" fullWidth>
        <DialogTitle>{detail?.type === 'reply' ? 'Text received' : 'Text sent'}</DialogTitle>
        <DialogContent dividers>
          {detail && detail.type === 'send' && <SendDetail entry={detail} />}
          {detail && detail.type === 'reply' && <ReplyDetail entry={detail} />}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDetail(null)}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

/**
 * The detail body for one outbound text.
 * @param props.entry - The send row.
 * @returns The detail stack.
 */
function SendDetail({ entry }: { entry: SendEntry }) {
  const answer = sendAnswerChip(entry);
  const delivery = deliveryChip(entry);
  const req = requestLabel(entry.request_kind, entry.request_item_id);
  return (
    <Stack spacing={2}>
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
        <Chip size="small" label={answer.label} color={answer.color} />
        {delivery && <Chip size="small" label={delivery.label} color={delivery.color} />}
        <Chip size="small" variant="outlined" label={entry.kind} />
      </Box>
      <Typography variant="body2">
        To {entry.to_number} from {entry.from_number || '(unknown)'}.
        {' '}Attempted {formatToronto(entry.at)}
        {entry.duration_ms !== null && ` (answered in ${entry.duration_ms} ms)`}
        {entry.http_status !== null && `, HTTP ${entry.http_status}`}
        {entry.twilio_status && `, Twilio status ${entry.twilio_status}`}
        {entry.twilio_sid && `, SID ${entry.twilio_sid}`}
        {req && `, for ${req}`}
      </Typography>
      {entry.error_message && <Alert severity="error">{entry.error_code ? `${entry.error_code}: ` : ''}{entry.error_message}</Alert>}
      {entry.no_response_reason && <Alert severity="warning">{entry.no_response_reason}</Alert>}
      <Box>
        <Typography variant="subtitle2">Message</Typography>
        <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{entry.body}</Typography>
      </Box>
      {entry.delivery_history.length > 0 && (
        <Box>
          <Typography variant="subtitle2">Delivery history</Typography>
          <Stack spacing={0.25}>
            {entry.delivery_history.map((h, i) => (
              <Typography key={i} variant="caption" color="text.secondary">
                {formatToronto(h.at)} — {h.status}{h.error_code ? ` (${h.error_code})` : ''}
              </Typography>
            ))}
          </Stack>
        </Box>
      )}
      {entry.response_body && (
        <Box>
          <Typography variant="subtitle2">Twilio response (verbatim)</Typography>
          <Box component="pre" sx={{ m: 0, mt: 1, p: 1.5, bgcolor: 'grey.100', overflowX: 'auto', fontSize: 12 }}>
            {entry.response_body}
          </Box>
        </Box>
      )}
    </Stack>
  );
}

/**
 * The detail body for one inbound text.
 * @param props.entry - The reply row.
 * @returns The detail stack.
 */
function ReplyDetail({ entry }: { entry: ReplyEntry }) {
  const outcome = OUTCOME_SPEC[entry.outcome] ?? { label: entry.outcome, color: 'default' as ChipColor };
  const req = requestLabel(entry.request_type, entry.item_id);
  return (
    <Stack spacing={2}>
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
        <Chip size="small" label={outcome.label} color={outcome.color} />
        {!entry.signature_ok && <Chip size="small" color="error" variant="outlined" label="Signature failed" />}
      </Box>
      <Typography variant="body2">
        From {entry.from_number || '(unknown)'} to {entry.to_number || '(unknown)'}.
        {' '}Received {formatToronto(entry.at)}
        {entry.message_sid && `, SID ${entry.message_sid}`}
        {entry.sender_staff_id && `, matched staff id ${entry.sender_staff_id}`}
      </Typography>
      <Typography variant="body2">
        Read as: {entry.decision || '(no decision)'}{req ? ` for ${req}` : ''}
        {entry.reason ? `, reason "${entry.reason}"` : ''}.
      </Typography>
      {entry.error_text && <Alert severity="warning">{entry.error_text}</Alert>}
      <Box>
        <Typography variant="subtitle2">Message</Typography>
        <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{entry.body}</Typography>
      </Box>
    </Stack>
  );
}
