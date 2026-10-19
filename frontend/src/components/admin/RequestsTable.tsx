import { Fragment, ReactNode, useEffect, useMemo, useState } from 'react';
import { Box, Button, Checkbox, Collapse } from '@mui/material';
import { Theme, alpha, useTheme } from '@mui/material/styles';
import KindPill from '../KindPill';
import TakenFrom from '../TakenFrom';
import RequestTimelineDialog from '../RequestTimelineDialog';
import { hasAuditLog } from '../dataGridDefaults';
import { DIAGNOSTIC_LABELS } from './requestRows';
import {
  COLUMNS, COLUMN_SETS, COLUMN_WIDTHS, Row, Src, amountText, num, priorityOrder, refDate, whenText,
} from './requestTable';
import { formatDay, formatInstantDay } from '../../utils/dates';
import { useFitColumns } from './useFitColumns';

/** The actions a row offers; each is wired to the dashboard's existing handlers. */
export interface TableActions {
  approve: (r: Row) => void;
  reject: (r: Row) => void;
  refund: (r: Row) => void;
  edit: (r: Row) => void;
  remind: (r: Row) => void;
  reprocess: (r: Row) => void;
  audit: (r: Row) => void;
  delete: (rows: Row[]) => void;
}

interface Props {
  rows: Row[];                       // already filtered and sorted, in final order
  src: Src | 'all';
  sort: { k: string; dir: 1 | -1 };
  onSort: (k: string) => void;
  sortable: boolean;                 // pending is fixed oldest-first, so headers do not sort
  group: string;                     // '', 'person', 'mgr', 'month', 'kind', 'status'
  openKey: string | null;
  onToggleOpen: (key: string) => void;
  selected: Set<string>;
  onSelect: (key: string, on: boolean) => void;
  onSelectAll: (keys: string[], on: boolean) => void;
  processingEnabled: boolean;
  busyKey: string | null;
  leaving: Set<string>;
  actions: TableActions;
  /** The word shown when nothing matches. */
  empty: string;
  /** A line under the table naming the dropped columns or the card fallback. */
  onFitNote: (note: string) => void;
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

/** Status to its text colour and soft background, matching the chips elsewhere. */
function statusTone(theme: Theme, status: string): { color: string; bg: string } {
  const p = theme.palette;
  const t = theme.tokens;
  if (status === 'Approved') return { color: p.success.main, bg: t.okSoft };
  if (status === 'Rejected' || status === 'Stuck') return { color: p.error.main, bg: t.errSoft };
  if (status === 'Refunded') return { color: p.info.main, bg: t.infoSoft };
  return { color: p.warning.main, bg: t.warnSoft };   // Pending and anything else
}

/** The totals line for a set of rows, by list. */
function totalsText(rows: Row[]): string {
  const parts: string[] = [];
  const lv = rows.filter((r) => r.src === 'leave');
  const mu = rows.filter((r) => r.src === 'mu');
  const co = rows.filter((r) => r.src === 'co');
  if (lv.length) parts.push(`${num(lv.reduce((a, r) => a + (r.days || 0), 0), 2)} days of leave`);
  if (mu.length) parts.push(`${num(mu.reduce((a, r) => a + (r.hours || 0), 0), 1)} make-up hours`);
  if (co.length) parts.push(`${num(co.reduce((a, r) => a + (r.days || 0), 0), 2)} carry-over and payout days`);
  return parts.length ? ` · ${parts.join(' · ')}` : '';
}

/** The group a row falls in, for the group-by control. */
function groupKey(r: Row, group: string): string {
  if (group === 'person') return r.person;
  if (group === 'mgr') return r.manager || 'No manager';
  if (group === 'kind') return r.kindLabel;
  if (group === 'status') return r.status;
  if (group === 'month') {
    const d = refDate(r);
    if (!d) return 'No date';
    const [y, m] = d.split('-').map(Number);
    return `${MONTHS[m - 1]} ${y}`;
  }
  return '';
}

/**
 * The rebuilt admin requests table (#279, #281): one table over the three
 * request lists, with sortable columns, grouping, selection, an inline detail
 * row per request and the inline approve/reject/refund actions. It never
 * scrolls sideways; when the window is narrow it drops the least important
 * columns and, narrower still, shows each request as a card (useFitColumns).
 *
 * @param props - See {@link Props}.
 * @returns The table.
 */
export default function RequestsTable(props: Props) {
  const { rows, src, sort, onSort, sortable, group, openKey, onToggleOpen, selected, processingEnabled, busyKey, leaving, actions, empty, onFitNote } = props;
  const theme = useTheme();
  const cols = COLUMN_SETS[src];
  const fit = useFitColumns(priorityOrder(src), COLUMN_WIDTHS);
  const hidden = useMemo(() => new Set(fit.hidden), [fit.hidden]);
  const cards = fit.cards;

  // Paint at most a page of rows at a time: thousands in the DOM freeze the tab.
  // Search, filters, sort, grouping and stats still run over the full list; only
  // how many rows are rendered is capped, with a "Show 100 more" control (#278).
  const PAGE = 100;
  const [limit, setLimit] = useState(PAGE);
  // Start again at one page when the row set, grouping or list changes.
  useEffect(() => { setLimit(PAGE); }, [rows.length, group, src]);

  // Tell the screen which columns are dropped (they stay in the opened request).
  // In an effect, not during render, so it never updates the parent mid-render.
  useEffect(() => {
    if (cards) { onFitNote('Shown as cards at this width. Open one for every column.'); return; }
    const names = cols.filter((k) => hidden.has(k)).map((k) => COLUMNS[k].label);
    onFitNote(names.length ? `Narrow screen: ${names.join(', ')} are in each opened request.` : '');
  }, [cards, hidden, cols, onFitNote]);

  /** Whether an action is running on this row. */
  const isBusy = (r: Row) => busyKey === `${r.request_type}-${r.id}` || busyKey === `reprocess-${r.id}`;
  /** Whether a decided pending row is fading out. */
  const isLeaving = (r: Row) => leaving.has(`${r.request_type}-${r.id}`);

  // The rows in display order, split into groups (one empty-key group when off).
  const groups = useMemo<[string, Row[]][]>(() => {
    if (!group) return [['', rows]];
    const map = new Map<string, Row[]>();
    rows.forEach((r) => { const k = groupKey(r, group); (map.get(k) || map.set(k, []).get(k)!).push(r); });
    return Array.from(map.entries());
  }, [rows, group]);

  // The rows actually painted, capped to `limit` across the groups in order.
  // The group and its per-group totals use the full group; only the slice that
  // is rendered is limited, so the DOM never holds thousands of rows at once.
  const renderGroups = useMemo(() => {
    let budget = limit;
    return groups.map(([gk, gr]) => {
      const show = budget > 0 ? gr.slice(0, budget) : [];  // this group's share of the page
      budget -= gr.length;
      return [gk, gr, show] as [string, Row[], Row[]];
    });
  }, [groups, limit]);
  const totalRows = rows.length;                           // the full matched set
  const renderedCount = Math.min(limit, totalRows);        // how many are painted
  const onMore = () => setLimit((n) => n + PAGE);          // reveal one more page

  // Select-all and the header checkbox act on EVERY matched row, not just the
  // painted ones, so a bulk action is correct for rows below the page cap.
  const shownKeys = rows.map((r) => r.key);
  const pickedShown = shownKeys.filter((k) => selected.has(k)).length;
  const allChecked = pickedShown > 0 && pickedShown === shownKeys.length;
  const someChecked = pickedShown > 0 && pickedShown < shownKeys.length;

  /** One data cell's content for a column key. */
  const cell = (r: Row, key: string): ReactNode => {
    switch (key) {
      case 'id': return r.id;
      case 'kind': return <KindPill kind={r.kind} label={r.kindLabel} />;
      case 'person': return (
        <Box>
          <Box sx={{ fontWeight: 700 }}>{r.person}</Box>
          <Box sx={{ fontSize: 12, color: 'text.secondary' }}>{[r.dept, r.loc].filter(Boolean).join(' · ')}</Box>
        </Box>
      );
      case 'what': return <Box sx={{ color: r.title ? 'text.primary' : 'text.secondary', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 1, WebkitBoxOrient: 'vertical' }}>{r.title || 'No description'}</Box>;
      case 'when': return whenText(r) || '-';
      case 'amount': return amountText(r);
      case 'taken': return <TakenFrom value={r.raw.taken_from} compact />;   // the taken_from the admin endpoints stamp

      case 'status': return <StatusCell r={r} theme={theme} />;
      case 'stage': return r.stage ? <Box component="span" sx={{ color: 'text.secondary' }}>{r.stage}</Box> : '-';
      case 'mgr': return r.manager ? `${r.manager}${r.managers.length > 1 ? ` +${r.managers.length - 1}` : ''}` : <Box sx={{ color: 'error.main' }}>None</Box>;
      case 'created': return formatInstantDay(r.created) || '-';   // the day it was created, e.g. "Oct 14, 2026"
      case 'approved': return r.approved ? formatInstantDay(r.approved) : <Box component="span" sx={{ color: 'text.secondary' }}>-</Box>;   // decided-at instant
      case 'start': return formatDay(r.start) || '-';     // calendar date
      case 'end': return formatDay(r.end) || '-';         // calendar date
      case 'days': return r.days == null ? '-' : num(r.days, 2);
      case 'date': return formatDay(r.date) || '-';       // overtime calendar date
      case 'hours': return r.hours == null ? '-' : num(r.hours, 1);
      case 'hdays': return <Box component="span" sx={{ color: 'text.secondary' }}>{num((r.hours || 0) / 8, 3)}</Box>;
      case 'newbal': return r.newBalance || <Box component="span" sx={{ color: 'text.secondary' }}>-</Box>;
      case 'sysstate': return <Box component="span" sx={{ color: r.sysState === 'Processed' ? 'text.secondary' : 'warning.main' }}>{r.sysState}</Box>;
      case 'quick': return <QuickActions r={r} processingEnabled={processingEnabled} busy={isBusy(r)} actions={actions} />;
      default: return '';
    }
  };

  const headCellSx = { p: '10px 12px', borderBottom: `1px solid ${theme.tokens.line}`, textAlign: 'left', whiteSpace: 'nowrap', position: 'sticky', top: 0, zIndex: 2, fontSize: 12.5, fontWeight: 700, color: 'text.secondary', bgcolor: theme.tokens.hover, userSelect: 'none' } as const;
  const bodyCellSx = { p: '10px 12px', borderBottom: `1px solid ${theme.tokens.line}`, textAlign: 'left', whiteSpace: 'nowrap', verticalAlign: 'middle' } as const;

  // Before the first width measure, a light placeholder keeps the container (and
  // its ref) in the DOM to be measured, without laying out every column at a guess.
  if (!fit.measured) return <Box ref={fit.ref} sx={{ overflow: 'clip', minHeight: 160 }} aria-hidden />;

  // ---- Cards layout: each request a labelled block (phones, or still too wide). ----
  if (cards) {
    return (
      <Box ref={fit.ref} sx={{ overflow: 'clip' }}>  {/* clip, not hidden: keeps the sticky toolbar above it working (#294 S9) */}
        {rows.length === 0 && <Box sx={{ p: 4, textAlign: 'center', color: 'text.secondary' }}>{empty}</Box>}
        {renderGroups.map(([gk, gr, show]) => (show.length === 0 ? null : (
          <Box key={gk || 'all'}>
            {gk && <Box sx={{ bgcolor: theme.tokens.chip, px: 1.75, py: 0.875, fontSize: 12.5, fontWeight: 700, color: 'text.secondary' }}>{gk} · {gr.length} {gr.length === 1 ? 'request' : 'requests'}{totalsText(gr)}</Box>}
            {show.map((r) => {
              const open = openKey === r.key;
              return (
                <Box key={r.key} id={`req-row-${r.key}`} sx={{ borderBottom: `1px solid ${theme.tokens.line}`, opacity: isLeaving(r) ? 0 : 1, transition: 'opacity .25s' }}>
                  <Box
                    role="button" tabIndex={0}
                    onClick={() => onToggleOpen(r.key)}
                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggleOpen(r.key); } }}
                    sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '6px 12px', p: '12px 14px', cursor: 'pointer', bgcolor: open ? theme.tokens.primarySoft : selected.has(r.key) ? alpha(theme.palette.secondary.main, 0.12) : 'transparent' }}
                  >
                    <Box sx={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: 1 }}>
                      <Checkbox size="small" checked={selected.has(r.key)} onClick={(e) => e.stopPropagation()} onChange={(e) => props.onSelect(r.key, e.target.checked)} sx={{ p: 0.5 }} inputProps={{ 'aria-label': `Select request ${r.id}` }} />
                      {cell(r, 'person')}
                    </Box>
                    {cols.filter((k) => k !== 'person' && k !== 'quick').map((k) => (
                      <Box key={k} sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
                        <Box sx={{ fontSize: 11, fontWeight: 700, color: 'text.secondary', textTransform: 'uppercase', letterSpacing: '.04em' }}>{COLUMNS[k].label}</Box>
                        {cell(r, k)}
                      </Box>
                    ))}
                    <Box sx={{ gridColumn: '1 / -1' }}>{cell(r, 'quick')}</Box>
                  </Box>
                  <Collapse in={open} unmountOnExit>
                    <Box sx={{ bgcolor: theme.tokens.primarySoft, px: 2, pb: 2 }}><RequestDetail r={r} processingEnabled={processingEnabled} busy={isBusy(r)} actions={actions} /></Box>
                  </Collapse>
                </Box>
              );
            })}
          </Box>
        )))}
        <MoreRows rendered={renderedCount} total={totalRows} step={PAGE} onMore={onMore} />
      </Box>
    );
  }

  // ---- Table layout ----
  const visibleCols = cols.filter((k) => !hidden.has(k));          // skip hidden cells, do not render them
  const colSpan = visibleCols.length + 2;                          // + selection + chevron
  return (
    <Box ref={fit.ref} sx={{ overflow: 'clip' }}>  {/* clip, not hidden: keeps the sticky header and toolbar working (#294 S9) */}
      <Box component="table" sx={{ borderCollapse: 'separate', borderSpacing: 0, width: '100%', fontSize: 14 }}>
        <Box component="thead">
          <Box component="tr">
            <Box component="th" sx={{ ...headCellSx, width: 34, pr: 0 }}>
              <Checkbox size="small" checked={allChecked} indeterminate={someChecked} onChange={(e) => props.onSelectAll(shownKeys, e.target.checked)} sx={{ p: 0.5 }} inputProps={{ 'aria-label': `Select all ${shownKeys.length} requests` }} />
            </Box>
            <Box component="th" sx={{ ...headCellSx, width: 24 }} />
            {visibleCols.map((k) => {
              const c = COLUMNS[k];
              const active = sortable && sort.k === k;
              return (
                <Box
                  key={k} component="th"
                  onClick={() => { if (!c.noSort && sortable) onSort(k); }}
                  role={!c.noSort && sortable ? 'button' : undefined}
                  tabIndex={!c.noSort && sortable ? 0 : undefined}
                  onKeyDown={(e) => { if ((e.key === 'Enter') && !c.noSort && sortable) onSort(k); }}
                  aria-sort={active ? (sort.dir > 0 ? 'ascending' : 'descending') : undefined}
                  sx={{ ...headCellSx, display: 'table-cell', textAlign: c.numeric ? 'right' : 'left', cursor: !c.noSort && sortable ? 'pointer' : 'default', '&:hover': { color: !c.noSort && sortable ? 'text.primary' : undefined } }}
                >
                  {c.label}{active && <Box component="span" sx={{ ml: 0.5, display: 'inline-block', transform: sort.dir > 0 ? 'none' : 'rotate(180deg)' }}>↑</Box>}
                </Box>
              );
            })}
          </Box>
        </Box>
        <Box component="tbody">
          {rows.length === 0 && (
            <Box component="tr"><Box component="td" colSpan={colSpan} sx={{ ...bodyCellSx, textAlign: 'center', color: 'text.secondary', py: 4, whiteSpace: 'normal' }}>{empty}</Box></Box>
          )}
          {renderGroups.map(([gk, gr, show]) => (show.length === 0 ? null : (
            <Fragment key={gk || 'all'}>
              {gk && (
                <Box component="tr"><Box component="td" colSpan={colSpan} sx={{ ...bodyCellSx, bgcolor: theme.tokens.chip, fontSize: 12.5, fontWeight: 700, color: 'text.secondary', whiteSpace: 'normal' }}>{gk} · {gr.length} {gr.length === 1 ? 'request' : 'requests'}{totalsText(gr)}</Box></Box>
              )}
              {show.map((r) => {
                const open = openKey === r.key;
                return (
                  <Fragment key={r.key}>
                    <Box
                      component="tr" role="button" tabIndex={0}
                      id={`req-row-${r.key}`}
                      aria-expanded={open}
                      onClick={() => onToggleOpen(r.key)}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggleOpen(r.key); } }}
                      sx={{ cursor: 'pointer', opacity: isLeaving(r) ? 0 : 1, transform: isLeaving(r) ? 'translateX(16px)' : 'none', transition: 'opacity .25s, transform .25s', bgcolor: open ? theme.tokens.primarySoft : selected.has(r.key) ? alpha(theme.palette.secondary.main, 0.12) : 'transparent', '&:hover': { bgcolor: open ? theme.tokens.primarySoft : theme.tokens.hover } }}
                    >
                      <Box component="td" sx={{ ...bodyCellSx, width: 34, pr: 0, borderBottomColor: open ? 'transparent' : theme.tokens.line }}>
                        <Checkbox size="small" checked={selected.has(r.key)} onClick={(e) => e.stopPropagation()} onChange={(e) => props.onSelect(r.key, e.target.checked)} sx={{ p: 0.5 }} inputProps={{ 'aria-label': `Select request ${r.id}` }} />
                      </Box>
                      <Box component="td" sx={{ ...bodyCellSx, borderBottomColor: open ? 'transparent' : theme.tokens.line }}>
                        <Box component="span" sx={{ display: 'inline-block', width: 18, color: open ? 'primary.main' : 'text.secondary', transition: 'transform .3s', transform: open ? 'rotate(90deg)' : 'none' }}>›</Box>
                      </Box>
                      {visibleCols.map((k) => (
                        <Box
                          key={k} component="td"
                          sx={{ ...bodyCellSx, display: 'table-cell', textAlign: COLUMNS[k].numeric ? 'right' : 'left', whiteSpace: COLUMNS[k].wrap ? 'normal' : 'nowrap', ...(COLUMNS[k].wrap ? { minWidth: 160, maxWidth: 340 } : {}), borderBottomColor: open ? 'transparent' : theme.tokens.line }}
                        >
                          {cell(r, k)}
                        </Box>
                      ))}
                    </Box>
                    <Box component="tr">
                      <Box component="td" colSpan={colSpan} sx={{ p: 0, borderBottom: `1px solid ${theme.tokens.line}`, bgcolor: open ? theme.tokens.primarySoft : 'transparent' }}>
                        <Collapse in={open} unmountOnExit>
                          <Box sx={{ px: { xs: 2, md: '42px' }, pt: 0.5, pb: 2.25 }}><RequestDetail r={r} processingEnabled={processingEnabled} busy={isBusy(r)} actions={actions} /></Box>
                        </Collapse>
                      </Box>
                    </Box>
                  </Fragment>
                );
              })}
            </Fragment>
          )))}
        </Box>
        {rows.length > 0 && (
          <Box component="tfoot">
            <Box component="tr"><Box component="td" colSpan={colSpan} sx={{ ...bodyCellSx, fontWeight: 700, bgcolor: theme.tokens.hover, borderBottom: 0, whiteSpace: 'normal' }}>Total{totalsText(rows)}</Box></Box>
          </Box>
        )}
      </Box>
      <MoreRows rendered={renderedCount} total={totalRows} step={PAGE} onMore={onMore} />
    </Box>
  );
}

/**
 * The "Show 100 more" control and the shown-of-total line under a capped table.
 * It appears only once the matched set is larger than one page, so a small list
 * carries no footer.
 *
 * @param props.rendered - How many rows are painted right now.
 * @param props.total - How many rows match in all.
 * @param props.step - The page size added by each click.
 * @param props.onMore - Reveal one more page.
 * @returns The control, or null when nothing is capped.
 */
function MoreRows({ rendered, total, step, onMore }: { rendered: number; total: number; step: number; onMore: () => void }) {
  const theme = useTheme();
  if (total <= step) return null;                          // one page or fewer: no footer needed
  return (
    <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', flexWrap: 'wrap', p: '10px 14px', borderTop: `1px solid ${theme.tokens.line}` }}>
      {rendered < total && (
        <Button size="small" variant="outlined" color="inherit" onClick={onMore}>Show {step} more</Button>
      )}
      <Box sx={{ fontSize: 13, color: 'text.secondary' }}>Showing {rendered.toLocaleString()} of {total.toLocaleString()}</Box>
    </Box>
  );
}

/** The status cell: the status pill plus a waiting badge and any stuck diagnostics. */
function StatusCell({ r, theme }: { r: Row; theme: Theme }) {
  const tone = statusTone(theme, r.status);
  const waited = r.waiting;
  return (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: '4px 8px', alignItems: 'center' }}>
      <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.625, fontSize: 12, fontWeight: 700, px: 1.125, py: 0.25, borderRadius: 999, color: tone.color, bgcolor: tone.bg }}>
        <Box component="span" sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: 'currentColor' }} />{r.status}
      </Box>
      {r.status === 'Pending' && r.source !== 'stuck' && waited != null && waited >= 2 && (
        <Box component="span" sx={{ fontSize: 11.5, fontWeight: 700, color: waited >= 7 ? 'error.main' : 'warning.main' }}>{waited}d</Box>
      )}
      {r.diagnostics.map((code) => {
        const d = DIAGNOSTIC_LABELS[code] || { label: code, color: 'warning' as const };
        return <Box key={code} component="span" sx={{ fontSize: 11.5, fontWeight: 700, px: 1, py: 0.25, borderRadius: 1, color: `${d.color}.main`, bgcolor: d.color === 'error' ? theme.tokens.errSoft : theme.tokens.warnSoft }}>{d.label}</Box>;
      })}
    </Box>
  );
}

/** The row-level quick actions: approve/reject a pending one, refund an approved one. */
function QuickActions({ r, processingEnabled, busy, actions }: { r: Row; processingEnabled: boolean; busy: boolean; actions: TableActions }) {
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  if (r.status === 'Approved') {
    return <Button size="small" variant="outlined" color="inherit" disabled={busy || !processingEnabled} onClick={(e) => { stop(e); actions.refund(r); }} sx={{ fontSize: 12.5 }}>Refund</Button>;
  }
  if (r.status !== 'Pending' || r.source === 'stuck') return null;
  return (
    <Box sx={{ display: 'inline-flex', gap: 0.5 }} onClick={stop}>
      <Button size="small" variant="outlined" color="success" disabled={busy || !processingEnabled} onClick={() => actions.approve(r)} aria-label={`Approve ${r.id}`} sx={{ minWidth: 32, px: 0 }}>✓</Button>
      <Button size="small" variant="outlined" color="error" disabled={busy || !processingEnabled} onClick={() => actions.reject(r)} aria-label={`Reject ${r.id}`} sx={{ minWidth: 32, px: 0 }}>✕</Button>
    </Box>
  );
}

/** One labelled field in the opened request. */
function Field({ label, value, wide }: { label: string; value: ReactNode; wide?: boolean }) {
  const empty = value === '' || value == null;
  return (
    <Box sx={{ minWidth: 0, gridColumn: wide ? '1 / -1' : undefined }}>
      <Box sx={{ fontSize: 11.5, color: 'text.secondary', fontWeight: 600 }}>{label}</Box>
      <Box sx={{ fontSize: 14, overflowWrap: 'anywhere', color: empty ? 'text.secondary' : 'text.primary', fontStyle: empty ? 'italic' : 'normal' }}>{empty ? 'Not set' : value}</Box>
    </Box>
  );
}

/**
 * The opened request: every column of its list on the left, what it did to the
 * person's balances on the right, and the actions the admin dashboard has today
 * plus Delete. The actions call the dashboard's existing handlers unchanged.
 *
 * @param props.r - The row.
 * @param props.processingEnabled - Whether writes are allowed.
 * @param props.busy - Whether an action is already running on this row.
 * @param props.actions - The handlers.
 * @returns The detail panel.
 */
function RequestDetail({ r, processingEnabled, busy, actions }: { r: Row; processingEnabled: boolean; busy: boolean; actions: TableActions }) {
  const theme = useTheme();
  const [timelineOpen, setTimelineOpen] = useState(false);   // the full timeline dialog (#329, #332)
  const listName = r.src === 'leave' ? 'Leave Requests' : r.src === 'mu' ? 'Overtime Requests Data' : 'CarryOver_Payout_Data';
  const title = r.src === 'leave' ? 'Leave request' : r.src === 'mu' ? 'Make-up request' : `${r.kindLabel} request`;
  const stuck = r.source === 'stuck' || r.sysState === 'Needs attention';

  const panelSx = { bgcolor: 'background.paper', border: `1px solid ${theme.tokens.line}`, borderRadius: 3.5, p: '14px 16px', display: 'grid', gap: 1.25, alignContent: 'start' } as const;

  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'minmax(0, 1.1fr) minmax(0, 1fr)' }, gap: '18px 24px', pt: 1 }}>
      <Box sx={panelSx}>
        <Box component="h3" sx={{ m: 0, fontSize: 16, display: 'flex', justifyContent: 'space-between', gap: 1, alignItems: 'baseline', fontFamily: theme.tokens.display }}>
          {title}<Box component="small" sx={{ fontSize: 12, color: 'text.secondary', fontWeight: 400, fontFamily: 'inherit' }}>{listName}</Box>
        </Box>
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '10px 14px' }}>
          <Field label="ID" value={r.id} />
          {r.src === 'leave' && <><Field label="Leave type" value={r.kindLabel} /><Field label="Start date" value={formatDay(r.start)} /><Field label="End date" value={formatDay(r.end)} /><Field label="Days" value={r.days == null ? '' : num(r.days, 2)} /></>}
          {r.src === 'mu' && <><Field label="Date" value={formatDay(r.date)} /><Field label="Hours" value={r.hours == null ? '' : num(r.hours, 1)} /><Field label="As days" value={`${num((r.hours || 0) / 8, 3)} (hours ÷ 8)`} /></>}
          {r.src === 'co' && <><Field label="Type of request" value={r.kindLabel} /><Field label="Days" value={r.days == null ? '' : num(r.days, 3)} /><Field label="New balance after this request" value={r.newBalance} wide /></>}
          <Field label="Status" value={r.status} />
          <Field label="Stage" value={r.stage} />
          <Field label="System state" value={r.sysState} />
          <Field label="Employee" value={r.person} />
          <Field label="Submitted by" value={r.submittedBy} />
          <Field label="Manager" value={r.manager} />
          {r.src === 'leave' && <Field label="All managers" value={r.managers.join(', ')} />}
          <Field label="Created" value={formatInstantDay(r.created)} />
          <Field label="Approved date" value={r.approved ? formatInstantDay(r.approved) : ''} />
          <Field label="Department" value={r.dept} />
          <Field label="Location" value={r.loc} />
          {r.src !== 'co' && <Field label={r.src === 'mu' ? 'Overtime details' : 'Title'} value={r.title} wide />}
        </Box>
        <DetailActions r={r} stuck={stuck} processingEnabled={processingEnabled} busy={busy} actions={actions} />
        {/* The request's full timeline and, for admins, its field history (#329, #332). */}
        <Button size="small" variant="text" onClick={() => setTimelineOpen(true)} sx={{ justifySelf: 'start' }}>View full timeline</Button>
        <RequestTimelineDialog
          open={timelineOpen}
          onClose={() => setTimelineOpen(false)}
          scope="admin"
          requestType={r.request_type}
          itemId={r.id}
        />
      </Box>

      <Box sx={panelSx}>
        {r.status === 'Pending' ? (
          // A pending request has no audit log yet, so show what approving it today would take, as chips.
          <>
            <Box component="h3" sx={{ m: 0, fontSize: 16, display: 'flex', justifyContent: 'space-between', gap: 1, alignItems: 'baseline', fontFamily: theme.tokens.display }}>
              What approving does<Box component="small" sx={{ fontSize: 12, color: 'text.secondary', fontWeight: 400, fontFamily: 'inherit' }}>From today's balances</Box>
            </Box>
            <TakenFrom value={r.raw.taken_from} />
          </>
        ) : (
          // Approved or refunded: the real movements, read from the balance audit log.
          <>
            <Box component="h3" sx={{ m: 0, fontSize: 16, display: 'flex', justifyContent: 'space-between', gap: 1, alignItems: 'baseline', fontFamily: theme.tokens.display }}>
              What it did to balances<Box component="small" sx={{ fontSize: 12, color: 'text.secondary', fontWeight: 400, fontFamily: 'inherit' }}>BalanceAuditLog</Box>
            </Box>
            <BalanceSteps r={r} onAudit={() => actions.audit(r)} />
          </>
        )}
      </Box>
    </Box>
  );
}

/** The actions shown in the opened request, by its state. */
function DetailActions({ r, stuck, processingEnabled, busy, actions }: { r: Row; stuck: boolean; processingEnabled: boolean; busy: boolean; actions: TableActions }) {
  const writeOff = !processingEnabled;
  return (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
      {stuck ? (
        <Button size="small" variant="contained" disabled={busy || writeOff} onClick={() => actions.reprocess(r)}>Reprocess</Button>
      ) : r.status === 'Pending' ? (
        <>
          <Button size="small" variant="contained" color="success" disabled={busy || writeOff} onClick={() => actions.approve(r)}>Approve</Button>
          <Button size="small" variant="outlined" color="error" disabled={busy || writeOff} onClick={() => actions.reject(r)}>Reject</Button>
          <Button size="small" variant="outlined" color="inherit" disabled={busy || writeOff} onClick={() => actions.edit(r)}>Edit</Button>
          <Button size="small" variant="outlined" color="inherit" disabled={busy || writeOff} onClick={() => actions.remind(r)}>Send a reminder</Button>
        </>
      ) : r.status === 'Approved' ? (
        <>
          <Button size="small" variant="outlined" color="error" disabled={busy || writeOff} onClick={() => actions.refund(r)}>Refund</Button>
          {hasAuditLog(r.raw) && <Button size="small" variant="outlined" color="inherit" onClick={() => actions.audit(r)}>Audit trail</Button>}
        </>
      ) : r.status === 'Refunded' && hasAuditLog(r.raw) ? (
        <Button size="small" variant="outlined" color="inherit" onClick={() => actions.audit(r)}>Audit trail</Button>
      ) : null}
      <Button size="small" color="error" disabled={writeOff} onClick={() => actions.delete([r])} sx={{ ml: 'auto' }}>Delete</Button>
    </Box>
  );
}

/** The balance audit log as numbered steps, or a message when there is none. */
function BalanceSteps({ r, onAudit }: { r: Row; onAudit: () => void }) {
  const theme = useTheme();
  let steps: { op: string; field: string; before: number; after: number }[] = [];
  if (r.auditLog) {
    try {
      const entries = JSON.parse(r.auditLog);
      if (Array.isArray(entries)) {
        entries.forEach((e: any) => (e.steps || []).forEach((s: any) => {
          const before = s.before || {};
          const after = s.after || {};
          const keys = Object.keys(before).length ? Object.keys(before) : Object.keys(after);
          if (!keys.length) { steps.push({ op: s.operation || 'Then', field: '', before: 0, after: 0 }); return; }
          keys.forEach((k, i) => steps.push({ op: i === 0 ? (s.operation || 'Then') : '', field: k, before: Number(before[k] ?? 0), after: Number(after[k] ?? 0) }));
        }));
      }
    } catch { steps = []; }
  }

  if (!steps.length) {
    const msg = r.status === 'Pending' ? 'Nothing yet. Balances change when it is approved.'
      : r.status === 'Rejected' ? 'Nothing. Rejected requests do not touch balances.'
      : 'No balance change recorded for this request.';
    return <Box sx={{ color: 'text.secondary' }}>{msg}</Box>;
  }

  return (
    <Box sx={{ display: 'grid' }}>
      {steps.map((s, i) => (
        <Box key={i} sx={{ display: 'grid', gridTemplateColumns: '22px minmax(0, 1fr)', gap: 1.25, pb: 1.5, position: 'relative', '&:not(:last-of-type)::before': { content: '""', position: 'absolute', left: 10, top: 20, bottom: 0, width: 2, bgcolor: theme.tokens.line } }}>
          <Box sx={{ width: 22, height: 22, borderRadius: '50%', bgcolor: 'primary.main', color: 'primary.contrastText', fontSize: 11.5, fontWeight: 700, display: 'grid', placeItems: 'center' }}>{i + 1}</Box>
          <Box>
            <Box sx={{ fontSize: 14, fontWeight: 600 }}>{s.op || 'Then'}</Box>
            {s.field && <Box sx={{ fontSize: 13, color: 'text.secondary', fontVariantNumeric: 'tabular-nums' }}>{s.field} <b style={{ color: theme.palette.text.primary }}>{num(s.before)} → {num(s.after)}</b> ({s.after - s.before > 0 ? '+' : ''}{num(s.after - s.before)})</Box>}
          </Box>
        </Box>
      ))}
      <Button size="small" variant="text" onClick={onAudit} sx={{ justifySelf: 'start' }}>Show the raw log</Button>
    </Box>
  );
}
