import { useEffect, useMemo, useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Box, Button, Checkbox, Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, FormControlLabel, MenuItem, Stack, TextField, ToggleButton, ToggleButtonGroup, Typography,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import ExportPeoplePicker from './ExportPeoplePicker';
import { Row, Src, num } from './requestTable';
import { formatTorontoDay, formatTorontoMoment } from '../../utils/dates';
import { takenFromText } from '../../utils/takenFrom';

/*
 * The requests export (#488), laid out like the staff export: which requests
 * (the shown or the ticked ones), which types (leave, make-up, carry-over and
 * payout; the table's own list first), which dates (a year, a month or a range,
 * #504), which of their people (each with a tick, #489), a column set or
 * single columns, one sheet or a sheet per type, an optional summary by
 * person, and a live preview. Every cell is worked out
 * here with the table's own words and Toronto dates (#490), so the preview is
 * exactly the file; the backend only writes the sheets it is sent.
 */

/** One cell value: text, a number, or blank. */
type Cell = string | number | null;

/** One export column: label, group, how it totals and wraps, and its value for a row. */
interface ColDef {
  key: string;
  label: string;
  group: string;
  number?: boolean;
  /** Summed in the totals row. */
  total?: boolean;
  /** Long text: wrapped in the file. */
  wrap?: boolean;
  get: (r: Row) => Cell;
}

/** A finished sheet, in the shape the export endpoints take. */
export interface ExportSheet {
  title: string;
  columns: { header: string; number: boolean; total: boolean; wrap: boolean }[];
  rows: Cell[][];
}

/** A leave or make-up row's first day: the leave start, or the make-up date. */
function startOf(r: Row): string {
  return r.src === 'mu' ? r.date : r.start;
}

/** A leave or make-up row's last day: the leave end (start when one day), or the make-up date. */
function endOf(r: Row): string {
  return r.src === 'mu' ? r.date : (r.end || r.start);
}

/** Every column the export offers, in the order it writes them. */
const COLUMNS: ColDef[] = [
  { key: 'id', label: 'Request #', group: 'Request', number: true, get: (r) => Number(r.id) || r.id },
  { key: 'kind', label: 'Type', group: 'Request', get: (r) => r.kindLabel },
  { key: 'what', label: 'Details', group: 'Request', wrap: true, get: (r) => r.title },
  { key: 'status', label: 'Status', group: 'Request', get: (r) => r.status },
  { key: 'stage', label: 'Stage', group: 'Request', wrap: true, get: (r) => r.stage },
  { key: 'person', label: 'Name', group: 'Person', get: (r) => r.person },
  { key: 'email', label: 'Email', group: 'Person', get: (r) => r.raw?.SubmitterEmail || '' },
  { key: 'dept', label: 'Department', group: 'Person', get: (r) => r.dept },
  { key: 'loc', label: 'Location', group: 'Person', get: (r) => r.loc },
  { key: 'emp', label: 'Salary or hourly', group: 'Person', get: (r) => r.employment_type || r.raw?.RequestEmploymentType || '' },
  { key: 'mgr', label: 'Manager', group: 'Person', wrap: true, get: (r) => r.managers.join(', ') },
  { key: 'start', label: 'Start date', group: 'Dates and amount', get: (r) => formatTorontoDay(startOf(r)) },
  { key: 'end', label: 'End date', group: 'Dates and amount', get: (r) => formatTorontoDay(endOf(r)) },
  // Leave and carry-over count days; make-up counts hours, so each sums on its own.
  { key: 'days', label: 'Days', group: 'Dates and amount', number: true, total: true, get: (r) => (r.src === 'mu' ? null : r.days) },
  { key: 'hours', label: 'Hours', group: 'Dates and amount', number: true, total: true, get: (r) => (r.src === 'mu' ? r.hours : null) },
  { key: 'taken', label: 'Taken from', group: 'Balance', wrap: true, get: (r) => takenFromText(r.raw?.taken_from) },
  { key: 'newbal', label: 'New balance', group: 'Balance', get: (r) => (r.src === 'co' ? r.newBalance : '') },
  { key: 'created', label: 'Created', group: 'Record', get: (r) => formatTorontoMoment(r.raw?.Created) },
  { key: 'approved', label: 'Approved date', group: 'Record', get: (r) => formatTorontoDay(r.raw?.ApprovedDate) },
  { key: 'modified', label: 'Last modified', group: 'Record', get: (r) => formatTorontoMoment(r.raw?.Modified) },
];
const GROUPS = ['Request', 'Person', 'Dates and amount', 'Balance', 'Record'];

/** Column presets, most used first. */
const PRESETS: [string, string[]][] = [
  ['Summary', ['person', 'kind', 'start', 'end', 'days', 'hours', 'status']],
  ['Balances', ['person', 'kind', 'days', 'hours', 'taken', 'status', 'approved']],
  ['Audit', ['id', 'person', 'kind', 'status', 'stage', 'mgr', 'created', 'approved', 'modified']],
  ['Everything', COLUMNS.map((c) => c.key)],
];

/** Name is always included (locked), so a row is never anonymous. */
const LOCKED = 'person';
/** How many chosen columns the preview shows before "and N more". */
const PREVIEW_COLS = 6;
/** How many rows the preview shows. */
const PREVIEW_ROWS = 12;
/** The per-type sheet titles, in the order they are written. */
const TYPE_SHEETS: [Src, string][] = [['leave', 'Leave'], ['mu', 'Make-up'], ['co', 'Carry-over and payout']];
/** Every request type, in the order the type filter lists them. */
const ALL_TYPES: Src[] = TYPE_SHEETS.map(([src]) => src);
const SUMMARY_SHEET = 'Summary by person';

/** Today as YYYY-MM-DD, for the download file name. */
function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** The date filter (#504): a year first, narrowed to a month or a custom range, or every date. */
type RangeKind = 'year' | 'month' | 'custom' | 'all';

/** Month names for the month picker, January first. */
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

/**
 * The first and last day (YYYY-MM-DD) a date filter covers; '' is open-ended.
 *
 * @param kind - Year, month, custom or all.
 * @param year - The picked year (year and month filters).
 * @param month - The picked month, 1 to 12 (month filter).
 * @param from - The custom first day, or ''.
 * @param to - The custom last day, or ''.
 * @returns [first, last]; both '' for all dates.
 */
function rangeBounds(kind: RangeKind, year: number, month: number, from: string, to: string): [string, string] {
  if (kind === 'year') return [`${year}-01-01`, `${year}-12-31`];
  if (kind === 'month') {
    const mm = String(month).padStart(2, '0');
    const lastDay = new Date(year, month, 0).getDate();      // day 0 of the next month = this month's last day
    return [`${year}-${mm}-01`, `${year}-${mm}-${String(lastDay).padStart(2, '0')}`];
  }
  if (kind === 'custom') return [from, to];                 // either may be blank: open on that side
  return ['', ''];                                          // all dates
}

/**
 * The days a request covers, as [first, last] YYYY-MM-DD.
 *
 * Leave runs start to end, make-up is its one day, and carry-over or payout
 * has no days of its own, so it is placed on the day it was made.
 *
 * @param r - The request row.
 * @returns [first, last]; '' when the row has no date at all.
 */
function rowSpan(r: Row): [string, string] {
  if (r.src === 'mu') return [r.date, r.date];               // make-up: one worked day
  if (r.src === 'co') return [r.created, r.created];         // carry-over/payout: the day it was made
  const first = r.start || r.created;                        // leave not dated yet: its created day
  return [first, r.end || first];                            // one-day leave has no end
}

/**
 * Whether a request has any day inside the range (dates compare as text).
 *
 * @param r - The request row.
 * @param first - The range's first day, or '' for no lower bound.
 * @param last - The range's last day, or '' for no upper bound.
 * @returns True when it overlaps; an undated row only passes an open range.
 */
function inRange(r: Row, first: string, last: string): boolean {
  if (!first && !last) return true;                          // all dates
  const [start, end] = rowSpan(r);
  if (!start) return false;                                  // no date to place it by
  return (!last || start <= last) && (!first || end >= first);   // overlap test
}

/**
 * The year a fresh dialog opens on: this year when any request touches it,
 * else the latest earlier year that has some, else the newest year at all.
 *
 * A list made last season (carry-over and payout made in Nov 2025, say) would
 * otherwise open on an empty year.
 *
 * @param years - The years the requests touch, newest first (always has this year).
 * @param touched - The years some request actually touches.
 * @param thisYear - This calendar year.
 * @returns The year to start on.
 */
function startYear(years: number[], touched: Set<number>, thisYear: number): number {
  if (touched.has(thisYear) || touched.size === 0) return thisYear;   // this year has some, or nothing at all
  const earlier = years.filter((y) => y < thisYear && touched.has(y));
  return earlier.length ? earlier[0] : years[0];                      // newest earlier year, else the newest
}

/** The key a person is ticked by: their staff id when the row has one, else their name. */
function personKey(r: Row): string {
  return String(r.raw?.EmployeeID || r.person);
}

/**
 * The summary sheet: one line per person with their counts and approved totals.
 *
 * @param rows - The requests going in the file.
 * @returns The sheet.
 */
function summarySheet(rows: Row[]): ExportSheet {
  const by = new Map<string, { name: string; dept: string; n: number; pending: number; leave: number; mu: number; co: number; po: number }>();
  rows.forEach((r) => {
    const k = personKey(r);
    const s = by.get(k) || { name: r.person, dept: r.dept, n: 0, pending: 0, leave: 0, mu: 0, co: 0, po: 0 };
    s.n += 1;
    if (r.status === 'Pending') s.pending += 1;
    if (r.status === 'Approved') {                         // totals count approved requests only
      if (r.src === 'leave') s.leave += r.days || 0;
      else if (r.src === 'mu') s.mu += r.hours || 0;
      else if (r.kindLabel === 'Payout') s.po += r.days || 0;
      else s.co += r.days || 0;
    }
    by.set(k, s);
  });
  const col = (header: string, number = false) => ({ header, number, total: number, wrap: false });
  return {
    title: SUMMARY_SHEET,
    columns: [col('Name'), col('Department'), col('Requests', true), col('Pending', true),
      col('Approved leave days', true), col('Approved make-up hours', true),
      col('Approved carry-over days', true), col('Approved payout days', true)],
    rows: Array.from(by.values())
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((s) => [s.name, s.dept, s.n, s.pending, s.leave, s.mu, s.co, s.po]),
  };
}

interface Props {
  open: boolean;
  /** The requests on screen (searched, filtered, sorted). */
  shownRows: Row[];
  /** The ticked requests, when the table offers ticking; empty or absent hides the choice. */
  tickedRows?: Row[];
  /**
   * The other lists' requests, when the table shows one list at a time, so the
   * type filter can add them; absent when the shown requests already hold every type.
   */
  otherRows?: Row[];
  /** Build the file from the sheets; resolves to the .xlsx. */
  build: (sheets: ExportSheet[]) => Promise<Blob>;
  /** The file name before the date, e.g. "requests" or "team-requests". */
  filePrefix?: string;
  onClose: () => void;
  onNotify: (message: string, severity: 'success' | 'error') => void;
}

/**
 * The requests export dialog: options on the left, a live preview on the right.
 *
 * @param props - See {@link Props}.
 * @returns The dialog.
 */
export default function RequestsExportDialog({ open, shownRows, tickedRows, otherRows, build, filePrefix = 'requests', onClose, onNotify }: Props) {
  const hasTicked = !!tickedRows && tickedRows.length > 0;
  const [which, setWhich] = useState<'shown' | 'ticked'>('shown');
  const [types, setTypes] = useState<Set<Src>>(new Set(ALL_TYPES));   // which request types go in
  const thisYear = Number(today().slice(0, 4));                  // this calendar year, on this device
  const [rangeKind, setRangeKind] = useState<RangeKind>('year');  // date filter (#504): a year first
  const [year, setYear] = useState(thisYear);                    // the picked year
  const [month, setMonth] = useState(new Date().getMonth() + 1);  // the picked month, 1 to 12
  const [from, setFrom] = useState('');                          // custom first day
  const [to, setTo] = useState('');                              // custom last day
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [columns, setColumns] = useState<string[]>(PRESETS[0][1].slice());   // start on "Summary"
  const [layout, setLayout] = useState<'one' | 'type'>('one');
  const [includeSummary, setIncludeSummary] = useState(false);
  const [tab, setTab] = useState('');
  const [downloading, setDownloading] = useState(false);

  // Each opening starts fresh: the ticked requests when there are some, the
  // table's own list as the types (all three on "All lists"), everyone ticked.
  useEffect(() => {
    if (!open) return;
    setWhich(hasTicked ? 'ticked' : 'shown');
    setExcluded(new Set());
    const onScreen = new Set(shownRows.map((r) => r.src));   // the types the table is showing
    setTypes(onScreen.size ? onScreen : new Set(ALL_TYPES));  // an empty table: every type
    setRangeKind('year');                                   // a year first; the effect below picks which
  }, [open]);   // eslint-disable-line react-hooks/exhaustive-deps

  // The requests to choose from: the ticked ones, or the shown ones plus the
  // other lists' (for the type filter); then only the ticked types.
  const pool = useMemo(
    () => (which === 'ticked' && hasTicked ? tickedRows! : [...shownRows, ...(otherRows || [])]),
    [which, hasTicked, tickedRows, shownRows, otherRows],
  );
  const typeCounts = useMemo(() => {
    const n: Record<Src, number> = { leave: 0, mu: 0, co: 0 };
    pool.forEach((r) => { n[r.src] += 1; });                  // how many of each type there are
    return n;
  }, [pool]);
  const picked = useMemo(() => pool.filter((r) => types.has(r.src)), [pool, types]);   // the requests chosen
  // The years those requests touch (touched), and the years to offer: those,
  // always with this year, newest first.
  const [years, touched] = useMemo(() => {
    const set = new Set<number>();
    picked.forEach((r) => {
      const [start, end] = rowSpan(r);
      if (!start) return;                                       // undated: no year to offer
      for (let y = Number(start.slice(0, 4)); y <= Number(end.slice(0, 4)); y += 1) set.add(y);   // a leave over New Year adds both
    });
    return [Array.from(new Set([thisYear, ...set])).sort((a, b) => b - a), set] as const;
  }, [picked, thisYear]);
  // A fresh dialog, or a change of types or of shown/ticked, lands on a year that has requests.
  useEffect(() => {
    if (open) setYear(startYear(years, touched, thisYear));
  }, [open, types, which]);   // eslint-disable-line react-hooks/exhaustive-deps

  /** Tick or untick one request type; the last one cannot be unticked. */
  const toggleType = (src: Src) => {
    setTypes((prev) => {
      const next = new Set(prev);
      if (next.has(src)) { if (next.size > 1) next.delete(src); } else next.add(src);
      return next;
    });
    setExcluded(new Set());                                  // a new set of people: everyone ticked
  };
  const [first, last] = rangeBounds(rangeKind, year, month, from, to);
  const badRange = !!first && !!last && first > last;          // custom from after to
  // Then only those with a day in the range, so the people list follows the dates too.
  const base = useMemo(
    () => (badRange ? [] : picked.filter((r) => inRange(r, first, last))),
    [picked, first, last, badRange],
  );
  // Their people, once each, in the order they first appear.
  const pickList = useMemo(() => {
    const seen = new Map<string, { key: string; name: string; detail: string }>();
    base.forEach((r) => {
      const k = personKey(r);
      if (!seen.has(k)) seen.set(k, { key: k, name: r.person, detail: [r.dept, r.loc].filter(Boolean).join(' · ') });
    });
    return Array.from(seen.values());
  }, [base]);
  const rows = useMemo(() => base.filter((r) => !excluded.has(personKey(r))), [base, excluded]);   // what goes in the file

  // The chosen columns in canonical order, name always in.
  const chosen = useMemo(() => {
    const set = new Set([LOCKED, ...columns]);
    return COLUMNS.filter((c) => set.has(c.key));
  }, [columns]);

  /** Toggle one column on or off (name cannot be turned off). */
  const toggleColumn = (key: string) => {
    if (key === LOCKED) return;
    setColumns((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  };

  // Switching to a range starts it on the picked year, so it narrows from there.
  useEffect(() => {
    if (rangeKind === 'custom' && !from && !to) { setFrom(`${year}-01-01`); setTo(`${year}-12-31`); }
  }, [rangeKind]);   // eslint-disable-line react-hooks/exhaustive-deps

  // The sheets exactly as they will be written; the preview reads these too.
  const sheets = useMemo<ExportSheet[]>(() => {
    const columnsOut = chosen.map((c) => ({ header: c.label, number: !!c.number, total: !!c.total, wrap: !!c.wrap }));
    const sheetOf = (title: string, list: Row[]): ExportSheet => ({ title, columns: columnsOut, rows: list.map((r) => chosen.map((c) => c.get(r))) });
    let out: ExportSheet[];
    if (layout === 'type') {
      out = TYPE_SHEETS.map(([src, title]) => sheetOf(title, rows.filter((r) => r.src === src))).filter((s) => s.rows.length);
      if (!out.length) out = [sheetOf('Requests', [])];   // nothing: one empty sheet so the file opens
    } else {
      out = [sheetOf('Requests', rows)];
    }
    if (includeSummary) out.push(summarySheet(rows));
    return out;
  }, [chosen, rows, layout, includeSummary]);

  // Keep the preview tab on a sheet that still exists.
  const current = sheets.find((s) => s.title === tab) || sheets[0];

  /** Build and download the workbook. */
  const download = async () => {
    setDownloading(true);
    try {
      const blob = await build(sheets);
      const url = URL.createObjectURL(blob);                 // the .xlsx blob
      const a = document.createElement('a');
      a.href = url;
      a.download = `${filePrefix}-${today()}.xlsx`;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      onNotify('Requests export downloaded.', 'success');
      onClose();
    } catch {
      onNotify('The export could not be built.', 'error');
    } finally {
      setDownloading(false);
    }
  };

  const previewCols = current.columns.slice(0, PREVIEW_COLS);
  const hiddenCols = current.columns.slice(PREVIEW_COLS);
  const people = new Set(rows.map(personKey)).size;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle>
        Export to Excel
        <Typography variant="body2" color="text.secondary">
          {filePrefix}-{today()}.xlsx · {rows.length} {rows.length === 1 ? 'request' : 'requests'} · {people} {people === 1 ? 'person' : 'people'} · {chosen.length} columns · {sheets.length} {sheets.length === 1 ? 'sheet' : 'sheets'}
        </Typography>
      </DialogTitle>
      <DialogContent dividers sx={{ p: 0 }}>
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: '340px minmax(0, 1fr)' } }}>
          {/* Options. Its single minmax(0, 1fr) track keeps every control inside
              the column: an auto track grew to the widest control (the two date
              fields side by side) and spilled the column over the preview. */}
          <Box sx={{ p: 2.5, minWidth: 0, borderRight: { md: 1 }, borderBottom: { xs: 1, md: 0 }, borderColor: 'divider', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 1.5, alignContent: 'start' }}>
            {hasTicked && (
              <>
                <Typography variant="overline" color="text.secondary">Requests</Typography>
                <ToggleButtonGroup exclusive size="small" value={which} onChange={(_, v) => { if (v) { setWhich(v); setExcluded(new Set()); } }} fullWidth>
                  <ToggleButton value="ticked">The {tickedRows!.length} ticked</ToggleButton>
                  <ToggleButton value="shown">The {shownRows.length} shown</ToggleButton>
                </ToggleButtonGroup>
              </>
            )}

            <Typography variant="overline" color="text.secondary">Types</Typography>
            <Box sx={{ display: 'grid' }}>
              {TYPE_SHEETS.map(([src, label]) => (
                <FormControlLabel
                  key={src}
                  control={<Checkbox size="small" checked={types.has(src)} onChange={() => toggleType(src)} />}
                  label={<Typography variant="body2">{label} <Typography component="span" variant="caption" color="text.secondary">({typeCounts[src]})</Typography></Typography>}
                />
              ))}
            </Box>

            <Typography variant="overline" color="text.secondary">Dates</Typography>
            <ToggleButtonGroup exclusive size="small" value={rangeKind} onChange={(_, v) => v && setRangeKind(v)} fullWidth sx={{ '& .MuiToggleButton-root': { minWidth: 0, px: 1 } }}>
              <ToggleButton value="year">Year</ToggleButton>
              <ToggleButton value="month">Month</ToggleButton>
              <ToggleButton value="custom">Range</ToggleButton>
              <ToggleButton value="all">All</ToggleButton>
            </ToggleButtonGroup>
            {(rangeKind === 'year' || rangeKind === 'month') && (
              <Stack direction="row" spacing={1} sx={{ '& > *': { minWidth: 0 } }}>
                <TextField select label="Year" size="small" value={years.includes(year) ? year : ''} onChange={(e) => setYear(Number(e.target.value))} fullWidth>
                  {years.map((y) => <MenuItem key={y} value={y}>{y}</MenuItem>)}
                </TextField>
                {rangeKind === 'month' && (
                  <TextField select label="Month" size="small" value={month} onChange={(e) => setMonth(Number(e.target.value))} fullWidth>
                    {MONTHS.map((m, i) => <MenuItem key={m} value={i + 1}>{m}</MenuItem>)}
                  </TextField>
                )}
              </Stack>
            )}
            {rangeKind === 'custom' && (
              <Stack direction="row" spacing={1} sx={{ '& > *': { minWidth: 0 } }}>
                <TextField label="From" type="date" size="small" value={from} onChange={(e) => setFrom(e.target.value)} InputLabelProps={{ shrink: true }} fullWidth />
                <TextField label="To" type="date" size="small" value={to} onChange={(e) => setTo(e.target.value)} InputLabelProps={{ shrink: true }} inputProps={{ min: from || undefined }} error={badRange} fullWidth />
              </Stack>
            )}
            <Typography variant="caption" color={badRange ? 'error' : 'text.secondary'}>
              {badRange ? 'The first day is after the last day.'
                : 'A request is in when any of its days fall in the range; carry-over and payout by the day they were made.'}
            </Typography>

            <Typography variant="overline" color="text.secondary">People</Typography>
            <ExportPeoplePicker people={pickList} excluded={excluded} onChange={setExcluded} />

            <Typography variant="overline" color="text.secondary">Columns</Typography>
            <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap">
              {PRESETS.map(([name, keys]) => {
                const on = keys.length === columns.length && keys.every((k) => columns.includes(k));
                return <Button key={name} size="small" variant={on ? 'contained' : 'outlined'} onClick={() => setColumns(keys.slice())}>{name}</Button>;
              })}
            </Stack>
            {GROUPS.map((group) => {
              const cols = COLUMNS.filter((c) => c.group === group);
              const on = cols.filter((c) => chosen.some((x) => x.key === c.key)).length;
              return (
                <Accordion key={group} disableGutters variant="outlined" sx={{ '&:before': { display: 'none' } }}>
                  <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                    <Typography sx={{ flex: 1, fontWeight: 600, fontSize: 14 }}>{group}</Typography>
                    <Typography variant="caption" color="text.secondary">{on} of {cols.length}</Typography>
                  </AccordionSummary>
                  <AccordionDetails sx={{ display: 'grid' }}>
                    {cols.map((c) => (
                      <FormControlLabel
                        key={c.key}
                        control={<Checkbox size="small" checked={chosen.some((x) => x.key === c.key)} disabled={c.key === LOCKED} onChange={() => toggleColumn(c.key)} />}
                        label={<Typography variant="body2">{c.label}{c.key === LOCKED ? ' (always)' : ''}</Typography>}
                      />
                    ))}
                  </AccordionDetails>
                </Accordion>
              );
            })}

            <Typography variant="overline" color="text.secondary">Sheets</Typography>
            <ToggleButtonGroup exclusive size="small" value={layout} onChange={(_, v) => v && setLayout(v)} fullWidth>
              <ToggleButton value="one">One sheet</ToggleButton>
              <ToggleButton value="type">A sheet per type</ToggleButton>
            </ToggleButtonGroup>
            <FormControlLabel control={<Checkbox size="small" checked={includeSummary} onChange={(e) => setIncludeSummary(e.target.checked)} />} label={<Typography variant="body2">Summary by person</Typography>} />
          </Box>

          {/* Preview */}
          <Box sx={{ p: 2.5, minWidth: 0 }}>
            <Typography variant="overline" color="text.secondary">Preview</Typography>
            {sheets.length > 1 && (
              <Stack direction="row" spacing={0.5} sx={{ my: 1 }} useFlexGap flexWrap="wrap">
                {sheets.map((s) => (
                  <Button key={s.title} size="small" variant={current.title === s.title ? 'contained' : 'text'} onClick={() => setTab(s.title)}>{s.title}</Button>
                ))}
              </Stack>
            )}

            <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 2, overflow: 'hidden' }}>
              <Box component="table" sx={{ width: '100%', tableLayout: 'fixed', borderCollapse: 'collapse', fontSize: 12.5, '& th, & td': { border: 1, borderColor: 'divider', p: '4px 8px', textAlign: 'left', verticalAlign: 'top', overflowWrap: 'anywhere', whiteSpace: 'normal' } }}>
                <Box component="thead"><Box component="tr" sx={{ bgcolor: 'action.hover' }}>
                  {previewCols.map((c) => <Box component="th" key={c.header}>{c.header}</Box>)}
                </Box></Box>
                <Box component="tbody">
                  {current.rows.slice(0, PREVIEW_ROWS).map((row, i) => (
                    <Box component="tr" key={i}>
                      {previewCols.map((c, j) => {
                        const v = row[j];
                        return (
                          <Box component="td" key={c.header} sx={{ textAlign: c.number ? 'right' : 'left', fontVariantNumeric: c.number ? 'tabular-nums' : undefined }}>
                            {typeof v === 'number' ? num(v) : v}
                          </Box>
                        );
                      })}
                    </Box>
                  ))}
                  {current.rows.length === 0 && (
                    <Box component="tr"><Box component="td" colSpan={previewCols.length}>
                      <Typography variant="body2" color="text.secondary">
                        {base.length === 0 && picked.length > 0 ? 'No requests fall in these dates.'
                          : 'No requests are ticked for export.'}
                      </Typography>
                    </Box></Box>
                  )}
                </Box>
              </Box>
            </Box>

            <Stack spacing={0.25} sx={{ mt: 1 }}>
              {hiddenCols.length > 0 && (
                <Typography variant="caption" color="text.secondary">and {hiddenCols.length} more column{hiddenCols.length === 1 ? '' : 's'} in the file: {hiddenCols.map((c) => c.header).join(', ')}</Typography>
              )}
              {current.rows.length > PREVIEW_ROWS && (
                <Typography variant="caption" color="text.secondary">Showing the first {PREVIEW_ROWS} of {current.rows.length} rows.</Typography>
              )}
              <Typography variant="caption" color="text.secondary">Header frozen with Excel filters; days and hours are numbers with a totals row; dates are Toronto days, as on the dashboard.</Typography>
            </Stack>
          </Box>
        </Box>
      </DialogContent>
      <Divider />
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={download} disabled={downloading || rows.length === 0}>
          {downloading ? 'Building...' : 'Download .xlsx'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
