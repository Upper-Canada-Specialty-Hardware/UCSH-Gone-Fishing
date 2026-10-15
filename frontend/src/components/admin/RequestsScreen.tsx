import { ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Box, Button, Collapse, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel,
  MenuItem, Paper, Snackbar, Switch, TextField, Typography,
} from '@mui/material';
import { Theme, useTheme } from '@mui/material/styles';
import KindPill from '../KindPill';
import AuditTrailDialog from '../AuditTrailDialog';
import PillToggle from '../PillToggle';
import { CountUp } from '../Motion';
import { DIAGNOSTIC_LABELS, HELD_STATUS_LABEL, HeldRow, OPEN_HELD, RequestView } from './requestRows';
import {
  COLUMN_SETS, FilterDef, Row, SRC_META, Src, columnValue, filterDefs, num, refDate, searchText, toRow, whenText,
} from './requestTable';
import RequestsTable, { TableActions } from './RequestsTable';
import RequestsCalendar from './RequestsCalendar';
import { useFitColumns } from './useFitColumns';
import DeleteRequestDialog from './DeleteRequestDialog';
import NewRequestDialog from './NewRequestDialog';
import { StaffOption, deleteAdminRequest, exportAdminRequests, flushDeleteBeacon, getNewRequestOptions } from '../../api/requestsAdmin';
import { getAdminHolidays } from '../../api/client';

/** How long a deleted request can be brought back before the server call lands. */
const UNDO_MS = 8000;

/** A request's identity across views: its list and item id, never the view it was read in. */
function delKey(r: Row): string {
  return `${r.request_type}-${r.id}`;                    // so deleting in Pending also hides it in All (#294 S10)
}

/** The view pills, left to right, with the label each shows. */
const VIEWS: { key: RequestView; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'pending', label: 'Pending' },
  { key: 'held', label: 'Held for new hires' },
  { key: 'stuck', label: 'Stuck' },
];

/** The list switch options. */
const SRC_OPTIONS: { key: Src | 'all'; label: string }[] = [
  { key: 'all', label: 'All lists' },
  { key: 'leave', label: 'Leave' },
  { key: 'mu', label: 'Make-up' },
  { key: 'co', label: 'Carry-over and payout' },
];

/** Today at local midnight, for the date-range filter. */
function startOfToday(): Date {
  const n = new Date();
  return new Date(n.getFullYear(), n.getMonth(), n.getDate());
}

/** A YYYY-MM-DD string to a Date at local midnight. */
function parseDate(s: string): Date {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
}

interface Props {
  view: RequestView;
  onView: (v: RequestView) => void;
  pending: any[];
  stuck: any[];
  requests: any[];
  held: HeldRow[];
  heldIncludeClosed: boolean;
  onHeldIncludeClosed: (v: boolean) => void;
  stats: any;
  processingEnabled: boolean;
  actionLoading: string | null;
  onApprove: (type: string, id: string) => Promise<void>;
  onReject: (type: string, id: string) => Promise<void>;
  onRemind: (type: string, id: string) => Promise<void>;
  onRefund: (type: string, id: string) => Promise<void>;
  onEdit: (item: any) => void;
  onReprocess: (id: string, reason: string) => Promise<void>;
  onAddHeld: (row: HeldRow) => void;
  onRetryHeld: (id: number) => Promise<void>;
  onCancelHeld: (id: number) => Promise<void>;
  /** Refetch the requests, pending, stuck and stats after a new request or a delete. */
  onReload: () => void | Promise<void>;
  leaving?: Set<string>;
}

/** The stat tiles across the strip; each reads the shown rows and can act on a click. */
interface StatDef {
  key: string;
  label: string;
  dp: number;
  color: (t: Theme) => string;
  value: (rows: Row[]) => number;
  onClick: 'clear' | { status: string } | Src;
  srcOnly?: Src;
}

/**
 * The admin requests screen (#278): one table (or calendar) over the three
 * SharePoint request lists, with a view (pending, held, stuck, all), a list
 * switch, a stat strip, search, a filter drawer, date range, grouping, row
 * selection with a command bar, inline actions, a new-request dialog and a
 * delete with Undo. The data and the approve/reject/refund/reprocess/held
 * actions are the dashboard's existing endpoints; new, delete, preview and
 * export are the screen's own.
 *
 * @param props - See {@link Props}.
 * @returns The requests screen.
 */
export default function RequestsScreen(props: Props) {
  const { view, onView, processingEnabled, actionLoading, onReload } = props;
  const theme = useTheme();

  const [srcSel, setSrcSel] = useState<Src | 'all'>('all');
  const [query, setQuery] = useState('');
  const [range, setRange] = useState('any');
  const [group, setGroup] = useState('');
  const [mode, setMode] = useState<'table' | 'cal'>('table');
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [filters, setFilters] = useState<Record<string, string[]>>({});
  const [sort, setSort] = useState<{ k: string; dir: 1 | -1 }>({ k: 'created', dir: -1 });
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [fitNote, setFitNote] = useState('');

  // Dialogs.
  const [deleteRows, setDeleteRows] = useState<Row[] | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [refundRows, setRefundRows] = useState<Row[] | null>(null);
  const [reprocessRow, setReprocessRow] = useState<Row | null>(null);
  const [reprocessReason, setReprocessReason] = useState('');
  const [auditLog, setAuditLog] = useState<string | null>(null);
  const [snack, setSnack] = useState<{ open: boolean; message: string }>({ open: false, message: '' });

  // Optimistic delete: hidden keys and the batch waiting out the Undo window.
  const [deletedKeys, setDeletedKeys] = useState<Set<string>>(new Set());
  const pendingDelete = useRef<{ rows: Row[]; reason: string; refund: boolean; timer: ReturnType<typeof setTimeout> } | null>(null);

  // New-request dialog options, loaded once the dialog is first opened.
  const [options, setOptions] = useState<{ staff: StaffOption[]; leave_types: string[] }>({ staff: [], leave_types: [] });
  // Company holidays for the calendar, loaded the first time it is shown.
  const [holidays, setHolidays] = useState<Record<string, string>>({});

  // A view change closes the open row, clears selection, and resets the list page.
  useEffect(() => { setOpenKey(null); setSelected(new Set()); }, [view]);

  /** Commit a pending delete batch to the server, or send it by beacon when leaving. */
  const flushDelete = useCallback((viaBeacon: boolean) => {
    const p = pendingDelete.current;
    if (!p) return;
    clearTimeout(p.timer);
    pendingDelete.current = null;
    const body = { reason: p.reason, refund: p.refund };
    if (viaBeacon) {
      p.rows.forEach((r) => flushDeleteBeacon(r.request_type, r.id, body));   // keepalive: survives the page unload
      return;
    }
    // Delete each on the server, keeping which succeeded. A failed delete means
    // the item is still there, so it must come back into view with a clear
    // reason, not disappear silently (#294 S10).
    Promise.all(p.rows.map((r) =>
      deleteAdminRequest(r.request_type, r.id, body)
        .then(() => ({ r, ok: true, detail: '' }))
        .catch((e) => ({ r, ok: false, detail: typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : '' })),
    )).then((results) => {
      const failed = results.filter((x) => !x.ok);
      // Bring the failed rows back now (the succeeded ones stay hidden until reload).
      setDeletedKeys((prev) => { const n = new Set(prev); failed.forEach((x) => n.delete(delKey(x.r))); return n; });
      if (failed.length) {
        const ids = failed.map((x) => x.r.id).join(', ');
        const detail = failed.find((x) => x.detail)?.detail || '';
        setSnack({ open: true, message: `Could not delete request${failed.length > 1 ? 's' : ''} ${ids}.${detail ? ` ${detail}` : ''}` });
      }
      Promise.resolve(onReload()).then(() => {
        // After the reload, the succeeded rows are gone from the data; drop their hidden keys.
        setDeletedKeys((prev) => { const n = new Set(prev); results.forEach((x) => { if (x.ok) n.delete(delKey(x.r)); }); return n; });
      });
    });
  }, [onReload]);

  // If the admin truly leaves during the Undo window, send the delete before the
  // page goes. Only pagehide, not visibilitychange: switching tabs is not leaving,
  // and firing the beacon then would commit a delete the admin could still undo.
  useEffect(() => {
    const onHide = () => { if (pendingDelete.current) flushDelete(true); };
    window.addEventListener('pagehide', onHide);
    return () => window.removeEventListener('pagehide', onHide);
  }, [flushDelete]);

  // All rows for the current view, flattened to the one row shape.
  const allRows = useMemo<Row[]>(() => {
    if (view === 'pending') return props.pending.map((r) => toRow(r, 'pending'));
    if (view === 'stuck') return props.stuck.map((r) => toRow(r, 'stuck'));
    return props.requests.map((r) => toRow(r, 'history'));           // 'all'
  }, [view, props.pending, props.stuck, props.requests]);

  // Rows left after the list switch and the optimistic deletes (the filter options read these).
  const scoped = useMemo(() => allRows.filter((r) => !deletedKeys.has(delKey(r)) && (srcSel === 'all' || r.src === srcSel)), [allRows, deletedKeys, srcSel]);
  const defs = useMemo<FilterDef[]>(() => filterDefs(scoped), [scoped]);

  /** Whether a row is inside the chosen date range. */
  const inRange = useCallback((r: Row): boolean => {
    if (range === 'any') return true;
    const d = refDate(r);
    if (!d) return false;
    const start = parseDate(d);
    const end = r.src === 'leave' && r.end ? parseDate(r.end) : start;
    const t = startOfToday();
    let a: Date; let b: Date;
    if (range === 'week') { a = new Date(t); a.setDate(a.getDate() - ((a.getDay() + 6) % 7)); b = new Date(a); b.setDate(b.getDate() + 6); }
    else if (range === 'month') { a = new Date(t.getFullYear(), t.getMonth(), 1); b = new Date(t.getFullYear(), t.getMonth() + 1, 0); }
    else if (range === 'next30') { a = new Date(t); b = new Date(t); b.setDate(b.getDate() + 30); }
    else { a = new Date(t.getFullYear(), 0, 1); b = new Date(t.getFullYear(), 11, 31); }  // year
    return end >= a && start <= b;
  }, [range]);

  /** Whether a row passes the search box and the active filter chips. */
  const matches = useCallback((r: Row): boolean => {
    const q = query.trim().toLowerCase();
    if (q && !searchText(r).includes(q)) return false;
    if (!inRange(r)) return false;
    return defs.every((f) => { const v = filters[f.key] || []; return !v.length || v.some((x) => f.test(r, x)); });
  }, [query, inRange, defs, filters]);

  // The rows on screen: scoped, searched, filtered, then sorted.
  const shown = useMemo(() => {
    const list = scoped.filter(matches);
    if (view === 'pending') return [...list].sort((a, b) => a.created.localeCompare(b.created));   // oldest first, fixed
    const k = COLUMN_SETS[srcSel].includes(sort.k) ? sort.k : 'created';
    return [...list].sort((a, b) => {
      const x = columnValue(a, k); const y = columnValue(b, k);
      if (typeof x === 'number' && typeof y === 'number') return (x - y) * sort.dir;
      return String(x || '').localeCompare(String(y || '')) * sort.dir || (Number(b.id) - Number(a.id));
    });
  }, [scoped, matches, view, srcSel, sort]);

  /** Change the sort column (toggle direction when it is already the one). */
  const onSort = useCallback((k: string) => {
    setSort((s) => ({ k, dir: s.k === k ? (s.dir === 1 ? -1 : 1) : (k === 'created' ? -1 : 1) }));
  }, []);

  // When the list switch changes, drop a sort column that list does not have.
  useEffect(() => {
    if (!COLUMN_SETS[srcSel].includes(sort.k)) setSort({ k: 'created', dir: -1 });
    setFilters({}); setOpenKey(null); setSelected(new Set());
  }, [srcSel]);  // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Actions wired to the dashboard's existing handlers. ----
  const actions: TableActions = useMemo(() => ({
    approve: (r) => props.onApprove(r.request_type, r.id),
    reject: (r) => props.onReject(r.request_type, r.id),
    refund: (r) => setRefundRows([r]),
    edit: (r) => props.onEdit(r.raw),
    remind: (r) => props.onRemind(r.request_type, r.id),
    reprocess: (r) => { setReprocessReason(''); setReprocessRow(r); },
    audit: (r) => setAuditLog(r.auditLog || r.raw.BalanceAuditLog || ''),
    delete: (rows) => setDeleteRows(rows),
  }), [props]);

  /** Tick or untick one row. */
  const selectOne = useCallback((key: string, on: boolean) => {
    setSelected((prev) => { const n = new Set(prev); if (on) n.add(key); else n.delete(key); return n; });
  }, []);
  /** Tick or untick every shown row. */
  const selectAll = useCallback((keys: string[], on: boolean) => {
    setSelected((prev) => { const n = new Set(prev); keys.forEach((k) => (on ? n.add(k) : n.delete(k))); return n; });
  }, []);

  const picked = useMemo(() => allRows.filter((r) => selected.has(r.key) && !deletedKeys.has(delKey(r))), [allRows, selected, deletedKeys]);

  // Del deletes the ticked rows, Esc clears them (not while typing or in a dialog).
  const anyDialogOpen = deleteRows !== null || newOpen || refundRows !== null || reprocessRow !== null || auditLog !== null;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!selected.size || anyDialogOpen) return;
      const el = document.activeElement as HTMLInputElement | null;
      const tag = el?.tagName || '';
      // A checkbox or radio keeps focus after a row is ticked, so the shortcut
      // must still work there; only a text field, select or textarea swallows it (#294 S8).
      const typing = (tag === 'INPUT' && el?.type !== 'checkbox' && el?.type !== 'radio') || tag === 'SELECT' || tag === 'TEXTAREA';
      if (typing) return;
      if (e.key === 'Delete') { e.preventDefault(); if (picked.length && processingEnabled) setDeleteRows(picked); }
      else if (e.key === 'Escape') setSelected(new Set());
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [selected, anyDialogOpen, picked, processingEnabled]);

  /** Download an .xlsx of the given request items. */
  const doExport = useCallback(async (items: any[]) => {
    try {
      const res = await exportAdminRequests(items);
      const url = URL.createObjectURL(res.data as Blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `requests-${startOfToday().toISOString().slice(0, 10)}.xlsx`;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch {
      setSnack({ open: true, message: 'The export could not be prepared.' });
    }
  }, []);

  /** Begin a delete: hide the rows, offer Undo, and send it to the server after the window. */
  const startDelete = useCallback((rows: Row[], reason: string, refund: boolean) => {
    if (pendingDelete.current) flushDelete(false);         // only one batch waits at a time
    setDeleteRows(null);
    const keys = rows.map((r) => r.key);
    setDeletedKeys((prev) => { const n = new Set(prev); keys.forEach((k) => n.add(k)); return n; });
    setSelected((prev) => { const n = new Set(prev); keys.forEach((k) => n.delete(k)); return n; });
    if (openKey && keys.includes(openKey)) setOpenKey(null);
    const timer = setTimeout(() => flushDelete(false), UNDO_MS);
    pendingDelete.current = { rows, reason, refund, timer };
    setSnack({ open: true, message: rows.length === 1 ? `Request ${rows[0].id} deleted.` : `${rows.length} requests deleted.` });
  }, [flushDelete, openKey]);

  /** Undo the waiting delete: cancel the server call and bring the rows back. */
  const undoDelete = useCallback(() => {
    const p = pendingDelete.current;
    if (!p) return;
    clearTimeout(p.timer);
    pendingDelete.current = null;
    setDeletedKeys((prev) => { const n = new Set(prev); p.rows.forEach((r) => n.delete(r.key)); return n; });
    setSnack({ open: false, message: '' });
  }, []);

  /** Open a request in the table (from the calendar or the command bar), and
   * scroll its row into view once the table has rendered. */
  const openInTable = useCallback((r: Row) => {
    setMode('table');
    setOpenKey(r.key);
    requestAnimationFrame(() => requestAnimationFrame(() => {   // wait for the table to lay out
      document.getElementById(`req-row-${r.key}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }));
  }, []);

  // Load the company holidays the first time the calendar is shown, so they mark
  // the grid; the calendar still works without them.
  useEffect(() => {
    if (mode !== 'cal' || Object.keys(holidays).length) return;
    getAdminHolidays().then((r) => {
      const map: Record<string, string> = {};
      (r.data.holidays || []).forEach((h: any) => {
        const d = (h.fields?.Date || '').slice(0, 10);         // the item's date, YYYY-MM-DD
        if (d) map[d] = h.fields?.Title || 'Holiday';
      });
      setHolidays(map);
    }).catch(() => { /* no marks is fine */ });
  }, [mode, holidays]);

  // Lazily load the new-request options the first time the dialog opens.
  const openNew = useCallback(() => {
    setNewOpen(true);
    if (!options.staff.length) {
      getNewRequestOptions().then((r) => setOptions(r.data)).catch(() => { /* the dialog still opens; the picker is just empty */ });
    }
  }, [options.staff.length]);

  // ---- The held view keeps its own simple table (a different data shape and actions). ----
  if (view === 'held') {
    return (
      <>
        <Paper sx={{ overflow: 'clip' }}>  {/* clip, not hidden, so the toolbar and table header can stick (#294 S9) */}
          <ViewTabs view={view} onView={onView} counts={{ all: props.requests.length, pending: props.pending.length, held: props.held.filter((h) => OPEN_HELD.includes(h.status)).length, stuck: props.stuck.length }} />
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', p: 1.5, borderBottom: `1px solid ${theme.tokens.line}` }}>
            <Typography color="text.secondary" sx={{ fontSize: 13, flex: 1 }}>Made on the request page by people not in the Staff Directory yet. Adding the person sends their requests on.</Typography>
            <FormControlLabel control={<Switch size="small" checked={props.heldIncludeClosed} onChange={(e) => props.onHeldIncludeClosed(e.target.checked)} />} label="Show sent and cancelled" />
          </Box>
          <HeldTable held={props.held} actionLoading={actionLoading} onAdd={props.onAddHeld} onRetry={props.onRetryHeld} onCancel={props.onCancelHeld} />
        </Paper>
      </>
    );
  }

  return (
    <>
      <Paper sx={{ overflow: 'clip' }}>  {/* clip, not hidden, so the toolbar and table header can stick (#294 S9) */}
        <ViewTabs view={view} onView={onView} counts={{ all: props.requests.length, pending: props.pending.length, held: props.held.filter((h) => OPEN_HELD.includes(h.status)).length, stuck: props.stuck.length }} />

        {/* The list switch and a line naming that SharePoint list. */}
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1.25, alignItems: 'center', p: '12px 14px', borderBottom: `1px solid ${theme.tokens.line}` }}>
          <PillToggle name="req-src" label="Which list" value={srcSel} onChange={(v) => setSrcSel(v)} options={SRC_OPTIONS} />
          <Typography color="text.secondary" sx={{ fontSize: 12.5 }}>{SRC_META[srcSel].note}</Typography>
        </Box>

        {/* The stat strip, each number counting to its new value. */}
        <StatStrip rows={shown} scoped={scoped} srcSel={srcSel} onStat={(s) => {
          if (s === 'clear') setFilters({});
          else if (typeof s === 'object') setFilters((f) => ({ ...f, status: [s.status] }));
          else setSrcSel(s);
        }} />

        {/* Toolbar, or the command bar when rows are ticked, in the same sticky spot. */}
        <Box sx={{ position: 'sticky', top: 0, zIndex: 4, bgcolor: 'background.paper' }}>
          {picked.length > 0 ? (
            <CommandBar
              picked={picked}
              processingEnabled={processingEnabled}
              onClear={() => setSelected(new Set())}
              onOpen={() => openInTable(picked[0])}
              onApprove={() => { Promise.all(picked.map((r) => props.onApprove(r.request_type, r.id))); setSelected(new Set()); }}
              onReject={() => { Promise.all(picked.map((r) => props.onReject(r.request_type, r.id))); setSelected(new Set()); }}
              onRefund={() => setRefundRows(picked)}
              onExport={() => doExport(picked.map((r) => r.raw))}
              onDelete={() => setDeleteRows(picked)}
            />
          ) : (
            <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', p: '10px 14px', borderBottom: `1px solid ${theme.tokens.line}` }}>
              <Button variant="contained" size="small" onClick={openNew} disabled={!processingEnabled}>+ New request</Button>
              <TextField
                size="small" type="search" value={query} placeholder="Search person, manager, details or ID"
                onChange={(e) => setQuery(e.target.value)} inputProps={{ 'aria-label': 'Search requests' }}
                sx={{ flex: '1 1 220px', minWidth: 0 }}
              />
              <Button size="small" variant={filtersOpen || activeCount(filters) ? 'contained' : 'outlined'} color={filtersOpen || activeCount(filters) ? 'primary' : 'inherit'} onClick={() => setFiltersOpen((o) => !o)} aria-expanded={filtersOpen}>
                Filters{activeCount(filters) ? ` (${activeCount(filters)})` : ''}
              </Button>
              <TextField select size="small" value={range} onChange={(e) => setRange(e.target.value)} aria-label="Dates" sx={{ minWidth: 130 }}>
                <MenuItem value="any">Any date</MenuItem>
                <MenuItem value="week">This week</MenuItem>
                <MenuItem value="month">This month</MenuItem>
                <MenuItem value="next30">Next 30 days</MenuItem>
                <MenuItem value="year">This year</MenuItem>
              </TextField>
              <TextField select size="small" value={group} onChange={(e) => setGroup(e.target.value)} aria-label="Group by" sx={{ minWidth: 140 }}>
                <MenuItem value="">No grouping</MenuItem>
                <MenuItem value="person">Group by person</MenuItem>
                <MenuItem value="mgr">Group by manager</MenuItem>
                <MenuItem value="month">Group by month</MenuItem>
                <MenuItem value="kind">Group by type</MenuItem>
                <MenuItem value="status">Group by status</MenuItem>
              </TextField>
              <PillToggle name="req-mode" label="Show as" value={mode} onChange={(v) => setMode(v)} options={[{ key: 'table', label: 'Table' }, { key: 'cal', label: 'Calendar' }]} />
              <Button size="small" variant="outlined" color="inherit" onClick={() => doExport(shown.map((r) => r.raw))}>Export to Excel</Button>
            </Box>
          )}
        </Box>

        {/* The filter drawer: a chip set per group. */}
        <Collapse in={filtersOpen}>
          <Box sx={{ p: '14px 16px', display: 'grid', gap: 1.5, bgcolor: theme.tokens.hover, borderBottom: `1px solid ${theme.tokens.line}` }}>
            {defs.filter((f) => f.options.length).map((f) => (
              <Box key={f.key} sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '120px minmax(0, 1fr)' }, gap: '8px 12px', alignItems: 'start' }}>
                <Box sx={{ fontSize: 12.5, fontWeight: 700, color: 'text.secondary', pt: 0.625 }}>{f.label}</Box>
                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75 }}>
                  {f.options.map((v) => {
                    const on = (filters[f.key] || []).includes(v);
                    return (
                      <Button key={v} size="small" variant={on ? 'contained' : 'outlined'} color={on ? 'primary' : 'inherit'} onClick={() => toggleFilter(setFilters, f.key, v)} sx={{ borderRadius: 999, py: 0.25 }}>{v}</Button>
                    );
                  })}
                </Box>
              </Box>
            ))}
          </Box>
        </Collapse>

        {/* The active-filter chips. */}
        {activeCount(filters) > 0 && (
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, alignItems: 'center', p: '8px 14px', borderBottom: `1px solid ${theme.tokens.line}`, fontSize: 13 }}>
            {defs.flatMap((f) => (filters[f.key] || []).map((v) => (
              <Box key={`${f.key}-${v}`} sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5, pl: 1.25, pr: 0.5, py: 0.375, borderRadius: 999, bgcolor: theme.tokens.primarySoft, color: 'primary.main', fontWeight: 600 }}>
                {f.label}: {v}
                <Box component="button" aria-label={`Remove filter ${v}`} onClick={() => toggleFilter(setFilters, f.key, v)} sx={{ border: 0, background: 'none', cursor: 'pointer', color: 'inherit', width: 20, height: 20, borderRadius: '50%', '&:hover': { bgcolor: 'background.paper' } }}>×</Box>
              </Box>
            )))}
            <Button size="small" variant="text" onClick={() => setFilters({})}>Clear all</Button>
          </Box>
        )}

        {/* Table or calendar. */}
        {mode === 'cal' ? (
          <RequestsCalendar rows={shown} holidays={holidays} onOpenRequest={openInTable} />
        ) : (
          <RequestsTable
            rows={shown}
            src={srcSel}
            sort={sort}
            onSort={onSort}
            sortable={view !== 'pending'}
            group={group}
            openKey={openKey}
            onToggleOpen={(k) => setOpenKey((cur) => (cur === k ? null : k))}
            selected={selected}
            onSelect={selectOne}
            onSelectAll={selectAll}
            processingEnabled={processingEnabled}
            busyKey={actionLoading}
            leaving={props.leaving || new Set()}
            actions={actions}
            empty={view === 'pending' ? 'No pending requests. Nothing is waiting on a manager.' : view === 'stuck' ? 'No stuck requests.' : 'No requests match.'}
            onFitNote={setFitNote}
          />
        )}

        {/* Footer: what the view is, and which columns stepped aside. */}
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, justifyContent: 'space-between', alignItems: 'center', p: '10px 14px', fontSize: 13, color: 'text.secondary', borderTop: `1px solid ${theme.tokens.line}` }}>
          <span>{footNote(view, props.stats)}</span>
          <span>{mode === 'table' ? fitNote : ''}</span>
        </Box>
      </Paper>

      <DeleteRequestDialog rows={deleteRows} onClose={() => setDeleteRows(null)} onConfirm={(reason, refund) => { if (deleteRows) startDelete(deleteRows, reason, refund); }} />

      <NewRequestDialog
        open={newOpen}
        onClose={() => setNewOpen(false)}
        defaultSrc={srcSel === 'all' ? 'leave' : srcSel}
        options={options}
        processingEnabled={processingEnabled}
        onDone={(res) => {
          setNewOpen(false);
          onReload();
          // A fallback-to-pending approval carries its own message to show (#294 S3).
          const message = res.message
            ? res.message
            : res.status === 'approved' ? `Request ${res.item_id} added and approved.` : `Request ${res.item_id} added.`;
          setSnack({ open: true, message });
        }}
        onError={(message) => setSnack({ open: true, message })}
      />

      {/* Refund confirm (one or several approved requests). */}
      <Dialog open={refundRows !== null} onClose={() => setRefundRows(null)} maxWidth="xs" fullWidth>
        <DialogTitle>{refundRows && refundRows.length === 1 ? `Refund request ${refundRows[0].id}?` : `Refund ${refundRows?.length} requests?`}</DialogTitle>
        <DialogContent dividers>
          <Box component="ul" sx={{ m: 0, pl: 0, listStyle: 'none', display: 'grid', gap: 0.75 }}>
            {(refundRows || []).slice(0, 6).map((r) => (
              <Box component="li" key={r.key} sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, alignItems: 'center', fontSize: 14 }}>
                <KindPill kind={r.kind} label={r.kindLabel} /><span>{r.person}{whenText(r) ? `, ${whenText(r)}` : ''}</span>
              </Box>
            ))}
            {refundRows && refundRows.length > 6 && <Box component="li" sx={{ color: 'text.secondary' }}>and {refundRows.length - 6} more</Box>}
          </Box>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>Puts the days back in each person's balance. The requests stay in the list, marked Refunded.</Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRefundRows(null)}>Cancel</Button>
          <Button variant="contained" color="warning" onClick={() => { const rows = refundRows || []; setRefundRows(null); setSelected(new Set()); Promise.all(rows.map((r) => props.onRefund(r.request_type, r.id))); }}>
            {refundRows && refundRows.length === 1 ? 'Refund' : `Refund ${refundRows?.length}`}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Reprocess needs a reason, kept in the processing log. */}
      <Dialog open={reprocessRow !== null} onClose={() => setReprocessRow(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Reprocess request {reprocessRow?.id}</DialogTitle>
        <DialogContent dividers>
          {reprocessRow && reprocessRow.diagnostics.length > 0 && (
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              {reprocessRow.diagnostics.map((c) => (DIAGNOSTIC_LABELS[c]?.label || c)).join(', ')}
            </Typography>
          )}
          <TextField label="Reason for reprocessing" placeholder="e.g. Fixed AllManagers field in Staff Directory" multiline rows={3} fullWidth required value={reprocessReason} onChange={(e) => setReprocessReason(e.target.value)} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setReprocessRow(null)}>Cancel</Button>
          <Button variant="contained" disabled={!reprocessReason.trim()} onClick={async () => { const r = reprocessRow!; setReprocessRow(null); await props.onReprocess(r.id, reprocessReason.trim()); }}>Reprocess</Button>
        </DialogActions>
      </Dialog>

      <AuditTrailDialog open={auditLog !== null} onClose={() => setAuditLog(null)} auditLog={auditLog || ''} />

      <Snackbar
        open={snack.open}
        autoHideDuration={pendingDelete.current ? UNDO_MS : 4000}
        onClose={() => setSnack((s) => ({ ...s, open: false }))}
        message={snack.message}
        action={pendingDelete.current ? <Button color="inherit" size="small" onClick={undoDelete}>Undo</Button> : undefined}
      />
    </>
  );
}

/** How many filter values are active in all. */
function activeCount(filters: Record<string, string[]>): number {
  return Object.values(filters).reduce((a, v) => a + v.length, 0);
}

/** Toggle one filter value on or off. */
function toggleFilter(setFilters: React.Dispatch<React.SetStateAction<Record<string, string[]>>>, key: string, value: string) {
  setFilters((f) => {
    const cur = f[key] || [];
    const next = cur.includes(value) ? cur.filter((x) => x !== value) : [...cur, value];
    return { ...f, [key]: next };
  });
}

/** The line at the bottom of the card for the current view. */
function footNote(view: RequestView, stats: any): string {
  if (view === 'pending') return 'Oldest first. The number beside a pending request is how many days it has waited; red after a week.';
  if (view === 'stuck') return 'Requests the system could not finish on its own. Fix the cause, then reprocess.';
  const t = stats?.total_requests;
  if (t) return `${(t.leave || 0).toLocaleString()} leave, ${(t.overtime || 0).toLocaleString()} overtime and ${(t.carryover_payout || 0).toLocaleString()} carry-over or payout requests in total.`;
  return 'Every request from the three lists.';
}

/** The view tabs with their counts. */
function ViewTabs({ view, onView, counts }: { view: RequestView; onView: (v: RequestView) => void; counts: Record<string, number> }) {
  const theme = useTheme();
  return (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.25, px: 1.5, borderBottom: `1px solid ${theme.tokens.line}` }}>
      {VIEWS.map((v) => {
        const on = v.key === view;
        return (
          <Box key={v.key} component="button" type="button" aria-pressed={on} onClick={() => onView(v.key)}
            sx={{ border: 0, background: 'none', p: '13px 12px', fontSize: 14, fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap', display: 'inline-flex', gap: 0.875, alignItems: 'center', color: on ? 'text.primary' : 'text.secondary', borderBottom: `3px solid ${on ? theme.palette.primary.main : 'transparent'}`, mb: '-1px' }}>
            {v.label}
            <Box component="span" sx={{ fontSize: 11.5, minWidth: 20, height: 20, borderRadius: 10, px: 0.75, display: 'inline-grid', placeItems: 'center', bgcolor: v.key === 'pending' ? theme.tokens.warnSoft : v.key === 'stuck' ? theme.tokens.errSoft : theme.tokens.chip, color: v.key === 'pending' ? 'warning.main' : v.key === 'stuck' ? 'error.main' : 'text.secondary' }}>{counts[v.key] ?? 0}</Box>
          </Box>
        );
      })}
    </Box>
  );
}

/**
 * The stat strip; each tile reads the shown rows and can filter or switch lists on a click.
 *
 * @param props.rows - The rows currently on screen (the tile's value).
 * @param props.scoped - All rows of the chosen list (the bar's full-height scale).
 * @param props.srcSel - The chosen list, so list-only tiles hide when one list is picked.
 * @param props.onStat - Called with what a tile does: clear filters, filter by status, or switch list.
 * @returns The stat strip.
 */
function StatStrip({ rows, scoped, srcSel, onStat }: { rows: Row[]; scoped: Row[]; srcSel: Src | 'all'; onStat: (s: 'clear' | { status: string } | Src) => void }) {
  const theme = useTheme();
  const notRej = (r: Row) => r.status !== 'Rejected' && r.status !== 'Refunded';
  const defs: StatDef[] = [
    { key: 'shown', label: 'Requests shown', dp: 0, color: (t) => t.palette.text.primary, value: (l) => l.length, onClick: 'clear' },
    { key: 'pending', label: 'Pending', dp: 0, color: (t) => t.palette.warning.main, value: (l) => l.filter((r) => r.status === 'Pending').length, onClick: { status: 'Pending' } },
    { key: 'leave', label: 'Leave days', dp: 1, color: (t) => t.palette.success.main, value: (l) => l.filter((r) => r.src === 'leave' && notRej(r)).reduce((a, r) => a + (r.days || 0), 0), onClick: 'leave', srcOnly: 'leave' },
    { key: 'mu', label: 'Make-up hours', dp: 1, color: (t) => t.palette.warning.main, value: (l) => l.filter((r) => r.src === 'mu' && r.status !== 'Rejected').reduce((a, r) => a + (r.hours || 0), 0), onClick: 'mu', srcOnly: 'mu' },
    { key: 'co', label: 'Carry-over days', dp: 2, color: (t) => t.palette.info.main, value: (l) => l.filter((r) => r.src === 'co' && r.kindLabel !== 'Payout' && r.status !== 'Rejected').reduce((a, r) => a + (r.days || 0), 0), onClick: 'co', srcOnly: 'co' },
    { key: 'po', label: 'Payout days', dp: 2, color: (t) => t.palette.success.main, value: (l) => l.filter((r) => r.src === 'co' && r.kindLabel === 'Payout' && r.status !== 'Rejected').reduce((a, r) => a + (r.days || 0), 0), onClick: 'co', srcOnly: 'co' },
  ];
  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', borderBottom: `1px solid ${theme.tokens.line}` }}>
      {defs.filter((s) => !(s.srcOnly && srcSel !== 'all' && srcSel !== s.srcOnly)).map((s) => {
        const val = s.value(rows);
        const max = Math.max(1, s.value(scoped));
        return (
          <Box key={s.key} component="button" type="button" onClick={() => onStat(s.onClick)}
            sx={{ p: '12px 16px', display: 'grid', gap: 0.25, borderRight: `1px solid ${theme.tokens.line}`, borderTop: 0, borderBottom: 0, borderLeft: 0, textAlign: 'left', cursor: 'pointer', background: 'none', color: s.color(theme), '&:hover': { bgcolor: theme.tokens.hover }, '&:last-of-type': { borderRight: 0 } }}>
            <Box sx={{ fontSize: 12, color: 'text.secondary', fontWeight: 600 }}>{s.label}</Box>
            <Box sx={{ fontSize: 24, fontWeight: 700, lineHeight: 1.15, color: 'text.primary' }}><CountUp value={val} format={(n) => num(n, s.dp)} /></Box>
            <Box sx={{ height: 4, borderRadius: 1, bgcolor: theme.tokens.line, overflow: 'hidden', mt: 0.5 }}><Box sx={{ height: '100%', borderRadius: 1, bgcolor: 'currentColor', width: `${Math.min(100, (val / max) * 100)}%`, transition: 'width .6s' }} /></Box>
          </Box>
        );
      })}
    </Box>
  );
}

/**
 * The selection command bar: it replaces the toolbar when rows are ticked.
 *
 * @param props.picked - The ticked rows (Approve/Reject show only when all are pending; Refund when all are approved).
 * @param props.processingEnabled - Whether writes are allowed; the write actions are disabled when off.
 * @param props.onClear - Clear the selection.
 * @param props.onOpen - Open the one ticked row.
 * @param props.onApprove - Approve all ticked rows.
 * @param props.onReject - Reject all ticked rows.
 * @param props.onRefund - Refund all ticked rows.
 * @param props.onExport - Export the ticked rows to Excel.
 * @param props.onDelete - Delete the ticked rows.
 * @returns The command bar.
 */
function CommandBar({ picked, processingEnabled, onClear, onOpen, onApprove, onReject, onRefund, onExport, onDelete }: {
  picked: Row[]; processingEnabled: boolean; onClear: () => void; onOpen: () => void; onApprove: () => void; onReject: () => void; onRefund: () => void; onExport: () => void; onDelete: () => void;
}) {
  const theme = useTheme();
  const allPending = picked.every((r) => r.status === 'Pending' && r.source !== 'stuck');
  const allApproved = picked.every((r) => r.status === 'Approved');
  return (
    <Box role="toolbar" aria-label="Selected requests" sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, alignItems: 'center', p: '8px 10px', minHeight: 55, borderBottom: `1px solid ${theme.tokens.line}`, bgcolor: theme.tokens.primarySoft }}>
      <Button size="small" onClick={onClear} aria-label="Clear selection (Esc)" sx={{ minWidth: 36 }}>✕</Button>
      <Box sx={{ fontWeight: 700, fontSize: 14, pl: 0.5, pr: 1.25, mr: 0.5, borderRight: `1px solid ${theme.tokens.line2}` }}>{picked.length} selected</Box>
      {picked.length === 1 && <Button size="small" color="inherit" onClick={onOpen}>Open</Button>}
      {allPending && <>
        <Button size="small" color="success" disabled={!processingEnabled} onClick={onApprove}>Approve</Button>
        <Button size="small" color="error" disabled={!processingEnabled} onClick={onReject}>Reject</Button>
      </>}
      {allApproved && <Button size="small" color="inherit" disabled={!processingEnabled} onClick={onRefund}>Refund</Button>}
      <Button size="small" color="inherit" onClick={onExport}>Export</Button>
      <Button size="small" color="error" disabled={!processingEnabled} onClick={onDelete}>Delete</Button>
      <Box sx={{ flex: '1 1 auto' }} />
      <Box sx={{ fontSize: 12, color: 'text.secondary', display: { xs: 'none', sm: 'block' } }}>Del to delete, Esc to clear</Box>
    </Box>
  );
}

/** The held table's columns and the assumed width each spends in the fit. */
const HELD_COLUMNS: { key: string; label: string; width: number }[] = [
  { key: 'person', label: 'Person', width: 200 },
  { key: 'department', label: 'Department', width: 130 },
  { key: 'supervisor', label: 'Supervisor', width: 150 },
  { key: 'request', label: 'Request', width: 220 },
  { key: 'status', label: 'Status', width: 170 },
  { key: 'actions', label: 'Actions', width: 170 },
];
/** Most important first: person, status and the actions are kept longest. */
const HELD_PRIORITY = ['person', 'status', 'actions', 'request', 'supervisor', 'department'];
const HELD_WIDTHS: Record<string, number> = Object.fromEntries(HELD_COLUMNS.map((c) => [c.key, c.width]));

/**
 * The held-for-new-hires table: a different shape and its own add/retry/cancel
 * actions. Like the main table it never scrolls sideways - it drops the
 * lowest-priority columns as it narrows (the actions are kept) and becomes
 * labelled cards on a phone (#294 S11).
 *
 * @param props.held - The held requests.
 * @param props.actionLoading - The key of the row an action is running on, or null.
 * @param props.onAdd - Add the new hire to the Staff Directory, releasing their requests.
 * @param props.onRetry - Retry a failed send.
 * @param props.onCancel - Cancel a held request.
 * @returns The held table.
 */
function HeldTable({ held, actionLoading, onAdd, onRetry, onCancel }: {
  held: HeldRow[]; actionLoading: string | null; onAdd: (h: HeldRow) => void; onRetry: (id: number) => Promise<void>; onCancel: (id: number) => Promise<void>;
}) {
  const theme = useTheme();
  const wrapRef = useRef<HTMLDivElement>(null);
  const fit = useFitColumns(wrapRef, HELD_PRIORITY, HELD_WIDTHS);
  const hidden = new Set(fit.hidden);
  const headSx = { p: '10px 12px', borderBottom: `1px solid ${theme.tokens.line}`, textAlign: 'left', fontSize: 12.5, fontWeight: 700, color: 'text.secondary', bgcolor: theme.tokens.hover } as const;
  const cellSx = { p: '10px 12px', borderBottom: `1px solid ${theme.tokens.line}`, textAlign: 'left', verticalAlign: 'top' } as const;

  /** One held row's cell content for a column key. */
  const cell = (h: HeldRow, key: string): ReactNode => {
    const busy = actionLoading === `held-${h.id}`;
    switch (key) {
      case 'person': return <><Box sx={{ fontWeight: 700 }}>{h.name}</Box><Box sx={{ fontSize: 12, color: 'text.secondary' }}>{h.email}</Box></>;
      case 'department': return h.department || '-';
      case 'supervisor': return h.supervisor_name || '-';
      case 'request': return h.summary;
      case 'status': return <>{HELD_STATUS_LABEL[h.status] ?? h.status}{h.last_error ? <Box sx={{ fontSize: 12, color: 'error.main' }}>{h.last_error}</Box> : null}</>;
      case 'actions': return (
        <Box sx={{ display: 'inline-flex', gap: 0.5, flexWrap: 'wrap' }}>
          {h.status === 'held' && <Button size="small" variant="contained" disabled={busy} onClick={() => onAdd(h)}>Add employee</Button>}
          {OPEN_HELD.includes(h.status) && <Button size="small" variant="outlined" color="inherit" disabled={busy} onClick={() => onRetry(h.id)}>Retry</Button>}
          {OPEN_HELD.includes(h.status) && <Button size="small" variant="outlined" color="error" disabled={busy} onClick={() => onCancel(h.id)}>Cancel</Button>}
        </Box>
      );
      default: return null;
    }
  };

  if (!held.length) return <Typography color="text.secondary" sx={{ p: 3 }}>Nothing is waiting.</Typography>;

  // Cards on a phone: each held request a labelled block, person and actions full width.
  if (fit.cards) {
    return (
      <Box ref={wrapRef} sx={{ overflow: 'clip' }}>
        {held.map((h) => (
          <Box key={h.id} sx={{ borderBottom: `1px solid ${theme.tokens.line}`, p: '12px 14px', display: 'grid', gap: '6px 12px', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
            <Box sx={{ gridColumn: '1 / -1' }}>{cell(h, 'person')}</Box>
            {['department', 'supervisor', 'request', 'status'].map((k) => (
              <Box key={k} sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
                <Box sx={{ fontSize: 11, fontWeight: 700, color: 'text.secondary', textTransform: 'uppercase', letterSpacing: '.04em' }}>{HELD_COLUMNS.find((c) => c.key === k)!.label}</Box>
                {cell(h, k)}
              </Box>
            ))}
            <Box sx={{ gridColumn: '1 / -1' }}>{cell(h, 'actions')}</Box>
          </Box>
        ))}
      </Box>
    );
  }

  return (
    <Box ref={wrapRef} sx={{ overflow: 'clip' }}>  {/* clip, not scroll: the table never goes sideways (#294 S11) */}
      <Box component="table" sx={{ borderCollapse: 'separate', borderSpacing: 0, width: '100%', fontSize: 14 }}>
        <Box component="thead"><Box component="tr">{HELD_COLUMNS.map((c) => <Box key={c.key} component="th" sx={{ ...headSx, display: hidden.has(c.key) ? 'none' : 'table-cell' }}>{c.label}</Box>)}</Box></Box>
        <Box component="tbody">
          {held.map((h) => (
            <Box component="tr" key={h.id}>
              {HELD_COLUMNS.map((c) => <Box key={c.key} component="td" sx={{ ...cellSx, display: hidden.has(c.key) ? 'none' : 'table-cell' }}>{cell(h, c.key)}</Box>)}
            </Box>
          ))}
        </Box>
      </Box>
    </Box>
  );
}
