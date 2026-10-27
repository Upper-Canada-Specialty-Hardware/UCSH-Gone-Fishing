import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box, Button, Dialog, DialogActions, DialogContent, DialogContentText, DialogTitle, MenuItem, Paper, Snackbar, TextField,
} from '@mui/material';
import { useTheme } from '@mui/material/styles';
import SplitRequestsTable from './admin/SplitRequestsTable';
import { TableActions } from './admin/RequestsTable';
import { Row, columnValue, columnsFor, refDate, searchText, toRow } from './admin/requestTable';
import RejectDialog from './RejectDialog';
import RequestsExportDialog, { ExportSheet } from './admin/RequestsExportDialog';
import { LiveScope } from '../api/events';
import { approveRequest, cancelMyRequest, exportTeamRequests, rejectRequest } from '../api/client';

/**
 * The employee History tab (#359) and the manager History tab (#360) share the
 * admin requests table: the same columns, the inline row detail with its
 * timeline and "Taken from" breakdown, the no-sideways-scroll fit and the phone
 * cards (components/admin/RequestsTable via SplitRequestsTable). This wrapper
 * gives it the toolbar those tabs need (search, status pills, a type filter, a
 * date range) and, by mode, the right actions:
 *
 * - mode "mine": an employee cannot approve, reject, refund or delete, so the
 *   only action is cancelling their own request while it still waits (#519).
 * - mode "team": a request still waiting on this manager can be approved or
 *   rejected in its row (the existing /team/approve and /team/reject routes);
 *   refund and delete stay admin-only. A team-scoped Excel export is offered.
 *
 * The rows come from the tab's existing data (/me/requests or /team/requests),
 * already carrying the taken_from breakdown, so there is no new read here. The
 * live-updates stream the dashboards already run merges a decision into those
 * rows, so approving from a row updates its status in place without a reload.
 */

/** The status filter pills, left to right: the status matched and its label. */
const STATUS_VIEWS: [string, string][] = [
  ['all', 'All'],
  ['Pending', 'Waiting'],
  ['Approved', 'Approved'],
  ['Rejected', 'Rejected'],
  ['Refunded', 'Refunded'],
  ['Cancelled', 'Cancelled'],
];

// One shared empty set for the selection/leaving props the table still takes but
// these read-only tables never use; a stable identity avoids needless re-renders.
const EMPTY_SET: Set<string> = new Set();

/** Today at local midnight, for the date-range filter. */
function startOfToday(): Date {
  const n = new Date();                                 // the reader's local now
  return new Date(n.getFullYear(), n.getMonth(), n.getDate());   // drop the time part
}

/** A YYYY-MM-DD string to a Date at local midnight. */
function parseDate(s: string): Date {
  const [y, m, d] = s.split('-').map(Number);           // split the date parts
  return new Date(y, m - 1, d);                         // month is 0-based
}

/**
 * Where another tab wants the table to land (#361): one request opened, or a
 * kind and status filtered. Each click passes a new object, so the same target
 * can be asked for twice in a row.
 */
export interface HistoryFocus {
  /** Open this request: its url-form type and id. */
  request_type?: string;
  id?: string | number;
  /** Filter to this kind label (falls back to a search when the label is not a filter option). */
  kind_label?: string;
  /** Filter to this status pill value ("Approved", "Pending" ...). */
  status?: string;
  /** Filter to one calendar month (#373): 0-based month and its year, used
   *  together. The manager Trends "days off per month" bars open History here. */
  month?: number;
  year?: number;
}

/** What the shared history table needs. */
interface Props {
  /** The requests as /me/requests or /team/requests returns them. */
  requests: any[];
  /** "mine" is the read-only employee table; "team" adds Person and approve/reject. */
  mode: 'mine' | 'team';
  /** Which timeline endpoint the opened request reads ('me' or 'team'). */
  timelineScope: LiveScope;
  /** Whether writes are allowed; gates the manager's approve/reject (team only). */
  processingEnabled?: boolean;
  /** Where another tab asked the table to land, or null. */
  focus?: HistoryFocus | null;
  /** Re-read the requests after a change made here (a cancel, mine only). */
  onChanged?: () => void;
}

/**
 * The employee or manager History tab's requests table.
 *
 * @param props - See {@link Props}.
 * @returns The toolbar, the status pills and the requests table in a card.
 */
export default function RequestsHistoryTable({ requests, mode, timelineScope, processingEnabled = false, focus = null, onChanged }: Props) {
  const theme = useTheme();
  const [query, setQuery] = useState('');                                    // the search box text
  const [statusView, setStatusView] = useState('all');                       // the active status pill
  const [kind, setKind] = useState('all');                                   // the type filter
  const [range, setRange] = useState('any');                                 // the date range
  // One specific calendar month to narrow to (#373), set by a Trends focus and
  // cleared when the reader touches the date dropdown. Separate from ``range``
  // because ``range`` only knows relative windows (this week, this month...).
  const [monthFocus, setMonthFocus] = useState<{ year: number; month: number } | null>(null);
  const [sort, setSort] = useState<{ k: string; dir: 1 | -1 }>({ k: 'when', dir: -1 });   // newest first by default
  const [openKey, setOpenKey] = useState<string | null>(null);               // the opened row, or none
  const [fitNote, setFitNote] = useState('');                                // the dropped-columns note
  const [busyKey, setBusyKey] = useState<string | null>(null);               // the row a decision is running on
  const [snack, setSnack] = useState({ open: false, message: '' });          // a short confirmation/error
  const [rejectRow, setRejectRow] = useState<Row | null>(null);              // the row awaiting a reject reason (#433)
  const [cancelRow, setCancelRow] = useState<Row | null>(null);              // the own request awaiting a cancel confirm (#519)

  // Every request flattened to the one table row shape; 'history' means no waiting badge.
  const allRows = useMemo<Row[]>(() => requests.map((r) => toRow(r, 'history')), [requests]);
  // The type-filter options: the distinct kinds actually present, sorted.
  const kindOptions = useMemo(() => Array.from(new Set(allRows.map((r) => r.kindLabel))).filter(Boolean).sort(), [allRows]);

  /** Whether a row's reference date falls inside the chosen range. */
  const inRange = useCallback((r: Row): boolean => {
    if (range === 'any') return true;                   // no date filter
    const d = refDate(r);                               // the date this row is placed on
    if (!d) return false;                               // a row with no date is outside any range
    const start = parseDate(d);
    const end = r.src === 'leave' && r.end ? parseDate(r.end) : start;   // leave spans start..end
    const t = startOfToday();
    let a: Date; let b: Date;
    if (range === 'week') { a = new Date(t); a.setDate(a.getDate() - ((a.getDay() + 6) % 7)); b = new Date(a); b.setDate(b.getDate() + 6); }
    else if (range === 'month') { a = new Date(t.getFullYear(), t.getMonth(), 1); b = new Date(t.getFullYear(), t.getMonth() + 1, 0); }
    else if (range === 'next30') { a = new Date(t); b = new Date(t); b.setDate(b.getDate() + 30); }
    else { a = new Date(t.getFullYear(), 0, 1); b = new Date(t.getFullYear(), 11, 31); }   // year
    return end >= a && start <= b;                      // overlaps the window
  }, [range]);

  /** Whether a row passes the search box, the status pill, the type filter and the range. */
  const matches = useCallback((r: Row): boolean => {
    const q = query.trim().toLowerCase();
    if (q && !searchText(r).includes(q)) return false;
    if (statusView !== 'all' && r.status !== statusView) return false;
    if (kind !== 'all' && r.kindLabel !== kind) return false;
    if (monthFocus) {
      // Narrow to one calendar month (#373): the row's span must overlap it.
      const d = refDate(r);                               // the date this row sits on
      if (!d) return false;                               // undated rows cannot be in a month
      const start = parseDate(d);
      const end = r.src === 'leave' && r.end ? parseDate(r.end) : start;   // leave spans start..end
      const a = new Date(monthFocus.year, monthFocus.month, 1);            // the month's first day
      const b = new Date(monthFocus.year, monthFocus.month + 1, 0);        // its last day
      if (!(end >= a && start <= b)) return false;        // no overlap with the month
    }
    return inRange(r);
  }, [query, statusView, kind, inRange, monthFocus]);

  // A click on another tab (#361) lands here: open one request, or filter to a
  // kind and status. Runs when a new focus object arrives; rows that are not
  // loaded yet simply leave the filters as they are.
  useEffect(() => {
    if (!focus) return;                                                     // nothing asked for
    setRange('any');                                                        // never hide the target by date
    // A month focus (#373) narrows to one calendar month; any other focus clears it.
    setMonthFocus(focus.month != null && focus.year != null ? { year: focus.year, month: focus.month } : null);
    if (focus.id != null && focus.request_type) {
      const hit = allRows.find((r) => r.request_type === focus.request_type && r.id === String(focus.id));
      setQuery('');                                                         // clear anything that could hide it
      setKind('all');
      setStatusView('all');
      if (hit) setOpenKey(hit.key);                                         // open its detail
      return;
    }
    if (focus.kind_label) {
      if (kindOptions.includes(focus.kind_label)) { setKind(focus.kind_label); setQuery(''); }
      else { setKind('all'); setQuery(focus.kind_label); }                  // not a filter option: search for it
    }
    if (focus.status) setStatusView(focus.status);                          // e.g. approved, for Trends totals
    setOpenKey(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus]);

  // The rows on screen: filtered, then sorted by the active column.
  const shown = useMemo(() => {
    const list = allRows.filter(matches);
    const k = columnsFor(mode, 'all').includes(sort.k) ? sort.k : 'when';   // only a column this table shows
    return [...list].sort((a, b) => {
      const x = columnValue(a, k); const y = columnValue(b, k);
      if (typeof x === 'number' && typeof y === 'number') return (x - y) * sort.dir;
      // Text sort, ties broken by the newest id, matching the admin table.
      return String(x || '').localeCompare(String(y || '')) * sort.dir || (Number(b.id) - Number(a.id));
    });
  }, [allRows, matches, mode, sort]);

  /** Change the sort column (toggle direction when it is already the one). */
  const onSort = useCallback((k: string) => {
    setSort((s) => ({ k, dir: s.k === k ? (s.dir === 1 ? -1 : 1) : (k === 'when' || k === 'created' ? -1 : 1) }));
  }, []);

  /**
   * Approve or reject one request from its row (manager only).
   *
   * Calls the same /team routes the Today queue uses, so the exactly-once rules
   * hold; the live stream then updates the row's status in place.
   *
   * @param r - The row.
   * @param yes - True to approve, false to reject.
   * @param reason - The reject reason (ignored when approving); required to reject (#433).
   */
  const decide = useCallback(async (r: Row, yes: boolean, reason = '') => {
    if (mode !== 'team') return;                         // the employee table never decides
    setBusyKey(`${r.request_type}-${r.id}`);             // disable this row's buttons while it runs
    try {
      await (yes ? approveRequest(r.request_type, r.id) : rejectRequest(r.request_type, r.id, reason));
      setSnack({ open: true, message: yes ? `Request ${r.id} approved.` : `Request ${r.id} rejected.` });
    } catch (err: any) {
      const detail = err?.response?.data?.detail;        // the backend's reason, when it sent one
      setSnack({ open: true, message: typeof detail === 'string' ? detail : (yes ? 'Approval failed.' : 'Rejection failed.') });
    } finally {
      setBusyKey(null);
    }
  }, [mode]);

  /**
   * Cancel one of the employee's own pending requests (#519).
   *
   * The server refuses one that is no longer pending (decided in the meantime)
   * with a reason, shown in the snackbar. On success the rows are re-read.
   *
   * @param r - The row, already confirmed.
   */
  const cancelMine = useCallback(async (r: Row) => {
    if (mode !== 'mine') return;                         // only the employee's own table cancels
    setBusyKey(`${r.request_type}-${r.id}`);             // disable this row's button while it runs
    try {
      await cancelMyRequest(r.request_type, r.id);
      setSnack({ open: true, message: `Request ${r.id} cancelled. Your manager was told.` });
      onChanged?.();                                     // show it as Cancelled straight away
    } catch (err: any) {
      const detail = err?.response?.data?.detail;        // e.g. "already approved"
      setSnack({ open: true, message: typeof detail === 'string' ? detail : 'The request could not be cancelled.' });
    } finally {
      setBusyKey(null);
    }
  }, [mode, onChanged]);

  // The tables still pass a full actions object; only approve/reject (team) and
  // cancel (mine) are ever reached, the rest are never rendered.
  // Rejecting opens the reason dialog first (#433); approving runs at once.
  const noop = useCallback(() => {}, []);
  const actions: TableActions = useMemo(() => ({
    approve: (r) => decide(r, true),
    reject: (r) => setRejectRow(r),
    refund: noop, edit: noop, remind: noop, reprocess: noop, audit: noop, delete: noop,
    cancel: mode === 'mine' ? (r) => setCancelRow(r) : undefined,   // asks first, then cancels
  }), [decide, noop, mode]);

  // The export dialog (#488): the manager picks people and columns from the rows on screen.
  const [exportOpen, setExportOpen] = useState(false);
  /** Write the dialog's sheets as a team-scoped .xlsx (manager only). */
  const buildExport = useCallback(async (sheets: ExportSheet[]) => (await exportTeamRequests(sheets)).data as Blob, []);

  const empty = mode === 'mine' ? 'No requests yet.' : 'No requests match.';

  return (
    <Paper sx={{ overflow: 'clip' }}>  {/* clip, not hidden, so the table's sticky header still works */}
      {/* Search, the type filter, the date range, and (manager only) Export. */}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', p: '12px 14px', borderBottom: `1px solid ${theme.tokens.line}` }}>
        <TextField
          size="small" type="search" value={query}
          placeholder={mode === 'team' ? 'Search person, details or ID' : 'Search details or ID'}
          onChange={(e) => setQuery(e.target.value)} inputProps={{ 'aria-label': 'Search requests' }}
          sx={{ flex: '1 1 220px', minWidth: 0 }}
        />
        <TextField select size="small" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Type" sx={{ minWidth: 150 }}>
          <MenuItem value="all">All types</MenuItem>
          {kindOptions.map((k) => <MenuItem key={k} value={k}>{k}</MenuItem>)}
        </TextField>
        <TextField select size="small" value={range} onChange={(e) => { setRange(e.target.value); setMonthFocus(null); }} aria-label="Dates" sx={{ minWidth: 130 }}>
          <MenuItem value="any">Any date</MenuItem>
          <MenuItem value="week">This week</MenuItem>
          <MenuItem value="month">This month</MenuItem>
          <MenuItem value="next30">Next 30 days</MenuItem>
          <MenuItem value="year">This year</MenuItem>
        </TextField>
        {/* A month picked from Trends (#373), shown as a removable filter. */}
        {monthFocus && (
          <Button
            size="small" variant="contained" color="primary" onClick={() => setMonthFocus(null)}
            aria-label={`Clear the month filter ${new Date(monthFocus.year, monthFocus.month, 1).toLocaleDateString('en-US', { month: 'short', year: 'numeric' })}`}
            sx={{ borderRadius: 999, py: 0.25 }}
          >
            {new Date(monthFocus.year, monthFocus.month, 1).toLocaleDateString('en-US', { month: 'short', year: 'numeric' })}
            <Box component="span" sx={{ ml: 0.75 }}>✕</Box>
          </Button>
        )}
        {mode === 'team' && (
          <Button size="small" variant="outlined" color="inherit" onClick={() => setExportOpen(true)}>Export to Excel</Button>
        )}
      </Box>

      {/* The status pills, each with its count across all this person's/team's requests. */}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, p: '10px 14px', borderBottom: `1px solid ${theme.tokens.line}` }}>
        {STATUS_VIEWS.map(([v, l]) => {
          const n = v === 'all' ? allRows.length : allRows.filter((r) => r.status === v).length;
          const on = statusView === v;
          return (
            <Button
              key={v} size="small" variant={on ? 'contained' : 'outlined'} color={on ? 'primary' : 'inherit'} aria-pressed={on}
              onClick={() => { setStatusView(v); setOpenKey(null); }} sx={{ borderRadius: 999, py: 0.25 }}
            >
              {l}<Box component="span" sx={{ ml: 0.75, opacity: 0.7, fontVariantNumeric: 'tabular-nums' }}>{n}</Box>
            </Button>
          );
        })}
      </Box>

      {/* The shared admin table: salaried and hourly split into their own tables (#338). */}
      <SplitRequestsTable
        rows={shown}
        src="all"
        sort={sort}
        onSort={onSort}
        sortable
        group=""
        openKey={openKey}
        onToggleOpen={(k) => setOpenKey((cur) => (cur === k ? null : k))}
        selected={EMPTY_SET}
        onSelect={noop}
        onSelectAll={noop}
        processingEnabled={mode === 'team' ? processingEnabled : false}
        busyKey={busyKey}
        leaving={EMPTY_SET}
        actions={actions}
        empty={empty}
        onFitNote={setFitNote}
        mode={mode}
        selectable={false}
        timelineScope={timelineScope}
      />

      {/* How many are shown of the whole set, and which columns stepped aside when narrow. */}
      <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 1, flexWrap: 'wrap', p: '10px 14px', fontSize: 13, color: 'text.secondary', borderTop: `1px solid ${theme.tokens.line}` }}>
        <span>{shown.length} of {allRows.length} {allRows.length === 1 ? 'request' : 'requests'}</span>
        <span>{fitNote}</span>
      </Box>

      {/* Every reject asks for a reason first (#433). */}
      <RejectDialog
        open={rejectRow !== null}
        personName={rejectRow?.person}
        requestLabel={rejectRow?.kindLabel}
        onCancel={() => setRejectRow(null)}
        onConfirm={(reason) => { const r = rejectRow; setRejectRow(null); if (r) void decide(r, false, reason); }}
      />

      <RequestsExportDialog
        open={exportOpen}
        shownRows={shown}
        build={buildExport}
        filePrefix="team-requests"
        onClose={() => setExportOpen(false)}
        onNotify={(message) => setSnack({ open: true, message })}
      />

      {/* Cancelling asks first: it cannot be undone from here (#519). */}
      <Dialog open={cancelRow !== null} onClose={() => setCancelRow(null)}>
        <DialogTitle>Cancel request {cancelRow?.id}?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            Your manager is told it was cancelled and nothing comes off your balances. To take the time off later, send a new request.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCancelRow(null)}>Keep it</Button>
          <Button color="error" variant="contained" onClick={() => { const r = cancelRow; setCancelRow(null); if (r) void cancelMine(r); }}>
            Cancel request
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar open={snack.open} autoHideDuration={4000} onClose={() => setSnack((s) => ({ ...s, open: false }))} message={snack.message} />
    </Paper>
  );
}
