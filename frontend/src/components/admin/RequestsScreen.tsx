import { useEffect, useMemo, useState } from 'react';
import {
  Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel,
  MenuItem, Paper, Switch, Table, TableBody, TableCell, TableHead, TablePagination, TableRow,
  TextField, Typography,
} from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import KindPill from '../KindPill';
import AuditTrailDialog from '../AuditTrailDialog';
import { STATUS_COLOR } from '../dataGridDefaults';
import RequestDrawer, { ActionButton, RequestActions, rowActions } from './RequestDrawer';
import {
  DIAGNOSTIC_LABELS, HeldRow, RequestRow, RequestView, TYPE_FILTERS, fromHeld, fromSharePoint,
} from './requestRows';

/** The view chips, in order, with their labels. */
const VIEWS: { key: RequestView; label: string }[] = [
  { key: 'pending', label: 'Pending' },
  { key: 'held', label: 'Held for new hires' },
  { key: 'stuck', label: 'Stuck' },
  { key: 'all', label: 'All' },
];

/** A line under the title saying what each view is for. */
const VIEW_NOTE: Record<RequestView, string> = {
  pending: 'Waiting on a manager, oldest first. Click a request for its balances, edit and reminders.',
  held: 'Made on the request page by people not in the Staff Directory yet. Adding the person sends their requests on. Their supervisor is reminded after 2 business days; admins are emailed after 5.',
  stuck: 'Requests the system could not finish on its own. Fix the cause, then reprocess.',
  all: '',
};

/** What to say when a view has nothing in it. */
const EMPTY: Record<RequestView, string> = {
  pending: 'No pending requests.',
  held: 'Nothing is waiting.',
  stuck: 'No stuck requests found.',
  all: 'No requests match.',
};

interface Props {
  view: RequestView;
  /** Switch view (changes the url). */
  onView: (v: RequestView) => void;
  pending: any[];
  stuck: any[];
  /** Every request, from /admin/requests. */
  requests: any[];
  held: HeldRow[];
  /** Held list: whether sent and cancelled ones are shown. */
  heldIncludeClosed: boolean;
  onHeldIncludeClosed: (v: boolean) => void;
  /** Request totals from /admin/stats, for the All view's line. */
  stats: any;
  processingEnabled: boolean;
  /** The key of the request an action is running on, as the dashboard sets it. */
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
}

/**
 * One requests table with four views: Pending, Held for new hires, Stuck and
 * All. Each row shows who, what, the manager, the status and how long it has
 * waited, with its main action at the end; clicking a row opens a side panel
 * with the details and every action. The data and actions are the same
 * endpoints the separate tabs used; only the presentation is new.
 *
 * @param props - See {@link Props}.
 * @returns The requests screen body.
 */
export default function RequestsScreen(props: Props) {
  const { view, onView, processingEnabled, actionLoading } = props;
  const theme = useTheme();
  const [type, setType] = useState('Any type');               // type filter
  const [query, setQuery] = useState('');                      // search box
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [openKey, setOpenKey] = useState<string | null>(null); // the row in the side panel
  const [reprocessRow, setReprocessRow] = useState<RequestRow | null>(null);
  const [reason, setReason] = useState('');
  const [refundRow, setRefundRow] = useState<RequestRow | null>(null);
  const [auditLog, setAuditLog] = useState<string | null>(null);

  // A view change (a chip, the sidebar, or the browser's Back) closes whatever was open.
  useEffect(() => {
    setOpenKey(null);
    setReprocessRow(null);
    setRefundRow(null);
    setPage(0);
  }, [view]);

  // Every source flattened into one row shape, per view.
  const byView = useMemo<Record<RequestView, RequestRow[]>>(() => ({
    pending: props.pending.map((r) => fromSharePoint(r, 'pending')),
    held: props.held.map(fromHeld),
    stuck: props.stuck.map((r) => fromSharePoint(r, 'stuck')),
    all: props.requests.map((r) => fromSharePoint(r, 'history')),
  }), [props.pending, props.held, props.stuck, props.requests]);

  /** Whether a row passes the type filter and the search. @param r - Row. @returns True to show it. */
  const matches = (r: RequestRow) => {
    const q = query.trim().toLowerCase();
    const text = `${r.who} #${r.id} ${r.what} ${r.note} ${r.manager}`.toLowerCase();
    return TYPE_FILTERS[type](r) && (!q || text.includes(q));
  };

  // The rows on screen: filtered, then oldest first while waiting, newest first in All.
  const rows = useMemo(() => {
    const list = byView[view].filter(matches);
    if (view === 'all') {
      return list.sort((a, b) => String(b.raw.StartDate || b.raw.Created || '').localeCompare(String(a.raw.StartDate || a.raw.Created || '')));
    }
    return list.sort((a, b) => (b.waiting ?? 0) - (a.waiting ?? 0));
  }, [byView, view, type, query]);

  // The panel's row is looked up fresh, so it closes itself once the row is gone (approved, reprocessed...).
  const openRow = useMemo(
    () => (openKey ? Object.values(byView).flat().find((r) => r.key === openKey) ?? null : null),
    [openKey, byView],
  );

  /** The dashboard's busy key for a row (it uses "<type>-<id>" and "reprocess-<id>"). @param r - Row. @returns Whether busy. */
  const isBusy = (r: RequestRow) =>
    actionLoading === `${r.request_type}-${r.id}` || actionLoading === `reprocess-${r.id}` || actionLoading === `held-${r.id}`;

  const actions: RequestActions = {
    approve: (r) => props.onApprove(r.request_type, r.id),
    reject: (r) => props.onReject(r.request_type, r.id),
    edit: (r) => props.onEdit(r.raw),
    remind: (r) => props.onRemind(r.request_type, r.id),
    refund: (r) => setRefundRow(r),                            // asks first
    reprocess: (r) => { setReason(''); setReprocessRow(r); },  // asks for a reason first
    audit: (r) => setAuditLog(r.raw.BalanceAuditLog),
    addHeld: (r) => props.onAddHeld(r.raw),
    retryHeld: (r) => props.onRetryHeld(r.raw.id),
    cancelHeld: (r) => props.onCancelHeld(r.raw.id),
  };

  /** The colour of a status chip. @param r - Row. @returns An MUI chip colour. */
  const statusColor = (r: RequestRow) => {
    if (r.source === 'stuck') return 'error';
    if (r.source === 'held') return r.raw.status === 'failed' ? 'error' : r.raw.status === 'held' ? 'warning' : 'default';
    return STATUS_COLOR[r.status] || 'default';
  };

  // The All view's line, from the stats endpoint's totals.
  const totals = props.stats?.total_requests;
  const note = view === 'all' && totals
    ? `${(totals.leave || 0).toLocaleString()} leave, ${(totals.overtime || 0).toLocaleString()} overtime and ${(totals.carryover_payout || 0).toLocaleString()} carry over or payout requests in total.`
    : VIEW_NOTE[view];
  const shown = rows.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage);

  return (
    <>
      {note && <Typography color="text.secondary" sx={{ mt: -1.5 }}>{note}</Typography>}

      <Paper sx={{ overflow: 'hidden' }}>
        {/* Toolbar: the four views with their counts, then the type filter and search. */}
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', p: 1.5, borderBottom: 1, borderColor: 'divider' }}>
          {VIEWS.map((v) => {
            const on = v.key === view;
            return (
              <Chip
                key={v.key}
                label={`${v.label} ${byView[v.key].filter(matches).length}`}
                onClick={() => { onView(v.key); setPage(0); }}
                aria-pressed={on}
                color={on ? 'primary' : 'default'}
                variant={on ? 'filled' : 'outlined'}
              />
            );
          })}
          <TextField
            select size="small" value={type} label="Type"
            onChange={(e) => { setType(e.target.value); setPage(0); }}
            sx={{ minWidth: 140, ml: { md: 'auto' } }}
          >
            {Object.keys(TYPE_FILTERS).map((t) => <MenuItem key={t} value={t}>{t}</MenuItem>)}
          </TextField>
          <TextField
            size="small" value={query} placeholder="Search name, #id or description"
            onChange={(e) => { setQuery(e.target.value); setPage(0); }}
            inputProps={{ 'aria-label': 'Search requests' }}
            sx={{ flex: { xs: '1 1 100%', sm: '0 1 280px' } }}
          />
          {view === 'held' && (
            <FormControlLabel
              control={<Switch size="small" checked={props.heldIncludeClosed} onChange={(e) => props.onHeldIncludeClosed(e.target.checked)} />}
              label="Show sent and cancelled"
            />
          )}
        </Box>

        {rows.length === 0 ? (
          <Typography color="text.secondary" sx={{ p: 3 }}>{EMPTY[view]}</Typography>
        ) : (
          <Box sx={{ overflowX: 'auto' }}>
            <Table size="small" sx={{ '& td, & th': { borderColor: 'divider' } }}>
              <TableHead>
                <TableRow>
                  <TableCell>Employee</TableCell>
                  <TableCell>Request</TableCell>
                  <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>Manager</TableCell>
                  <TableCell>Status</TableCell>
                  {view !== 'all' && <TableCell align="right" sx={{ display: { xs: 'none', sm: 'table-cell' } }}>Waiting</TableCell>}
                  {/* Phones: the row's button is left to the side panel. */}
                  <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }} />
                </TableRow>
              </TableHead>
              <TableBody>
                {shown.map((r) => {
                  const main = rowActions(r)[0];               // the row shows its main action only
                  const late = r.waiting !== null && r.waiting > 7;
                  return (
                    <TableRow
                      key={r.key}
                      hover
                      tabIndex={0}
                      onClick={() => setOpenKey(r.key)}
                      onKeyDown={(e) => { if (e.key === 'Enter') setOpenKey(r.key); }}
                      sx={{ cursor: 'pointer' }}
                    >
                      <TableCell>
                        <Typography sx={{ fontWeight: 600, whiteSpace: { sm: 'nowrap' } }}>{r.who}</Typography>
                        <Typography variant="caption" color="text.secondary">{r.source === 'held' ? r.raw.email : `#${r.id}`}</Typography>
                      </TableCell>
                      <TableCell sx={{ minWidth: { sm: 200 } }}>
                        <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                          <KindPill kind={r.kind} label={r.what} />
                          <Typography variant="body2">{r.when}</Typography>
                        </Box>
                        {/* Under it: why it is stuck, or what the person wrote. */}
                        {r.issues.length > 0 ? (
                          <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap', mt: 0.5 }}>
                            {r.issues.map((c) => {
                              const d = DIAGNOSTIC_LABELS[c] || { label: c, color: 'warning' as const };
                              return <Chip key={c} size="small" color={d.color} variant="outlined" label={d.label} />;
                            })}
                          </Box>
                        ) : r.note ? (
                          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', maxWidth: 360 }} noWrap>{r.note}</Typography>
                        ) : null}
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        {r.manager || <Chip size="small" color="error" variant="outlined" label="None" />}
                      </TableCell>
                      <TableCell><Chip size="small" color={statusColor(r)} label={r.status} /></TableCell>
                      {view !== 'all' && (
                        <TableCell align="right" sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                          {/* Over a week is late: warn colour. */}
                          <Box component="span" sx={{ px: 1, py: 0.25, borderRadius: 999, fontWeight: 700, fontSize: 12, color: late ? 'warning.main' : 'text.secondary', bgcolor: late ? alpha(theme.palette.warning.main, 0.12) : 'transparent' }}>
                            {r.waiting === null ? '' : r.waiting === 0 ? 'today' : `${r.waiting} d`}
                          </Box>
                        </TableCell>
                      )}
                      <TableCell align="right" sx={{ whiteSpace: 'nowrap', display: { xs: 'none', sm: 'table-cell' } }}>
                        {main && (
                          <ActionButton
                            tone={main.tone}
                            label={main.label}
                            disabled={isBusy(r) || (main.writes && !processingEnabled)}
                            onClick={() => actions[main.act](r)}
                          />
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </Box>
        )}
        {rows.length > 25 && (
          <TablePagination
            component="div"
            count={rows.length}
            page={page}
            onPageChange={(_, p) => setPage(p)}
            rowsPerPage={rowsPerPage}
            onRowsPerPageChange={(e) => { setRowsPerPage(Number(e.target.value)); setPage(0); }}
            rowsPerPageOptions={[25, 50, 100]}
          />
        )}
      </Paper>

      <RequestDrawer
        row={openRow}
        onClose={() => setOpenKey(null)}
        actions={actions}
        processingEnabled={processingEnabled}
        busy={!!openRow && isBusy(openRow)}
      />

      {/* Reprocess needs a reason, kept in the processing log. */}
      <Dialog open={!!reprocessRow} onClose={() => setReprocessRow(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Reprocess request #{reprocessRow?.id}</DialogTitle>
        <DialogContent dividers>
          {reprocessRow?.detail && <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{reprocessRow.detail}</Typography>}
          <TextField
            label="Reason for reprocessing"
            placeholder="e.g. Fixed AllManagers field in Staff Directory"
            multiline rows={3} fullWidth required
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setReprocessRow(null)}>Cancel</Button>
          <Button
            variant="contained"
            disabled={!reason.trim() || (!!reprocessRow && isBusy(reprocessRow))}
            onClick={async () => { await props.onReprocess(reprocessRow!.id, reason.trim()); setReprocessRow(null); }}
          >
            Reprocess
          </Button>
        </DialogActions>
      </Dialog>

      {/* Refund puts back what approval took, so it asks first. */}
      <Dialog open={!!refundRow} onClose={() => setRefundRow(null)} maxWidth="xs" fullWidth>
        <DialogTitle>Refund this request?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            {refundRow?.who}: {refundRow?.what} {refundRow?.when}. The balance change from its approval is reversed and it is marked Refunded.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRefundRow(null)}>Keep it</Button>
          <Button
            variant="contained" color="warning"
            onClick={async () => { const r = refundRow!; setRefundRow(null); await props.onRefund(r.request_type, r.id); }}
          >
            Refund
          </Button>
        </DialogActions>
      </Dialog>

      <AuditTrailDialog open={auditLog !== null} onClose={() => setAuditLog(null)} auditLog={auditLog || ''} />
    </>
  );
}
