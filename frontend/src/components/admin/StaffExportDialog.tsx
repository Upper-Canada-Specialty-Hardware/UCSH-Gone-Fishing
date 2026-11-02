import { useEffect, useMemo, useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Box, Button, Checkbox, Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, FormControlLabel, MenuItem, Stack, TextField, ToggleButton,
  ToggleButtonGroup, Typography,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import { exportStaff } from '../../api/staff';
import ExportPeoplePicker from './ExportPeoplePicker';

/*
 * The staff export preview (#277). HR picks the people (unticking any of the
 * shown ones, #489), a column set or single columns, and the extra sheets, sees
 * a live preview of the first rows, then downloads the .xlsx the backend builds. The preview never scrolls sideways:
 * cells wrap and only the first few chosen columns show, with "and N more".
 */

/** A person as /admin/balances returns them, used for the live preview. */
export interface ExportPerson {
  id: string;
  name: string;
  email: string;
  department: string;
  location: string;
  employment_type: string;
  system_check?: string;
  balances: Record<string, number>;
  managers: string[];
}

/** One export column: label, group, number flag, and a preview getter. */
interface ColDef {
  key: string;
  label: string;
  group: string;
  number?: boolean;
  /** Value for the preview from a list person ('' when the list lacks it). */
  get: (p: ExportPerson) => string | number;
}

/** The export columns, mirroring staff_export.COLUMNS on the backend. */
const COLUMNS: ColDef[] = [
  { key: 'id', label: 'ID', group: 'Identity', number: true, get: (p) => p.id },
  { key: 'name', label: 'Name', group: 'Identity', get: (p) => p.name },
  { key: 'email', label: 'Email', group: 'Identity', get: (p) => p.email },
  { key: 'cell', label: 'Cell', group: 'Contact', get: () => '' },
  { key: 'extension', label: 'Extension', group: 'Contact', get: () => '' },
  { key: 'birthday', label: 'Birthday', group: 'Contact', get: () => '' },
  { key: 'location', label: 'Location', group: 'Placement', get: (p) => p.location },
  { key: 'department', label: 'Department', group: 'Placement', get: (p) => p.department },
  { key: 'salary_hourly', label: 'Salary or hourly', group: 'Placement', get: (p) => p.employment_type },
  { key: 'supervisor_text', label: 'Supervisor (text)', group: 'Placement', get: () => '' },
  { key: 'managers', label: 'Managers', group: 'Placement', get: (p) => (p.managers || []).join(', ') },
  { key: 'vacation_entitlement', label: 'Vacation entitlement', group: 'Entitlements and balances', number: true, get: (p) => p.balances?.vacation_entitlement ?? 0 },
  { key: 'vacation', label: 'Vacation', group: 'Entitlements and balances', number: true, get: (p) => p.balances?.vacation_balance ?? 0 },
  { key: 'sick_entitlement', label: 'Sick entitlement', group: 'Entitlements and balances', number: true, get: (p) => p.balances?.sick_entitlement ?? 0 },
  { key: 'sick', label: 'Sick', group: 'Entitlements and balances', number: true, get: (p) => p.balances?.sick_balance ?? 0 },
  { key: 'make_up_days', label: 'Make-up (days)', group: 'Entitlements and balances', number: true, get: (p) => p.balances?.overtime ?? 0 },
  { key: 'make_up_hours', label: 'Make-up (hours)', group: 'Entitlements and balances', number: true, get: (p) => (p.balances?.overtime ?? 0) * 8 },
  { key: 'carry_over', label: 'Carry-over', group: 'Entitlements and balances', number: true, get: (p) => p.balances?.carryover ?? 0 },
  { key: 'payout', label: 'Payout', group: 'Entitlements and balances', number: true, get: (p) => p.balances?.payout ?? 0 },
  { key: 'system_check', label: 'System check', group: 'Status', get: (p) => p.system_check || '' },
  { key: 'request_allow_date', label: 'Can book vacation until', group: 'Status', get: () => '' },
  { key: 'comments', label: 'Comments', group: 'Record', get: () => '' },
  { key: 'modified', label: 'Last modified', group: 'Record', get: () => '' },
];
const COL_BY_KEY: Record<string, ColDef> = Object.fromEntries(COLUMNS.map((c) => [c.key, c]));
const GROUPS = ['Identity', 'Contact', 'Placement', 'Entitlements and balances', 'Status', 'Record'];

/** Column presets, matching the mockup. */
const PRESETS: [string, string[]][] = [
  ['Everything', COLUMNS.map((c) => c.key)],
  ['Balances', ['name', 'department', 'managers', 'vacation_entitlement', 'vacation', 'sick_entitlement', 'sick', 'make_up_days', 'make_up_hours', 'carry_over', 'payout', 'request_allow_date']],
  ['Contact list', ['name', 'email', 'cell', 'extension', 'location', 'department']],
  ['Setup check', ['id', 'name', 'email', 'managers', 'salary_hourly', 'system_check', 'modified']],
];

/** Name is always included (locked), so a row is never anonymous. */
const LOCKED = 'name';
/** How many chosen columns the preview shows before "and N more". */
const PREVIEW_COLS = 6;
/** How many rows the preview shows. */
const PREVIEW_ROWS = 12;

/** Today as YYYY-MM-DD, for the download file name. */
function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** A number rounded to two decimals for the preview. */
function previewNum(v: number): number {
  return Math.round(v * 100) / 100;
}

interface Props {
  open: boolean;
  /** The shown (filtered, sorted) people. */
  shownPeople: ExportPerson[];
  /** Everyone, for the "Everyone" choice. */
  allPeople: ExportPerson[];
  onClose: () => void;
  onNotify: (message: string, severity: 'success' | 'error') => void;
}

/**
 * The export dialog: options on the left, a live preview on the right.
 *
 * @param props - See {@link Props}.
 * @returns The dialog.
 */
export default function StaffExportDialog({ open, shownPeople, allPeople, onClose, onNotify }: Props) {
  const [who, setWho] = useState<'shown' | 'all'>('shown');
  const [columns, setColumns] = useState<string[]>(PRESETS[1][1].slice());   // start on "Balances"
  const [includeSummary, setIncludeSummary] = useState(true);
  const [includeHistory, setIncludeHistory] = useState(false);
  const [tab, setTab] = useState('Staff');
  const [downloading, setDownloading] = useState(false);
  // The shown people HR unticked (#489); kept as exclusions so anyone new starts ticked.
  const [excluded, setExcluded] = useState<Set<string>>(new Set());

  // Each time the dialog opens it starts with everyone shown ticked again.
  useEffect(() => { if (open) setExcluded(new Set()); }, [open]);

  // The people the file lists: the shown ones still ticked, or everyone.
  const people = useMemo(
    () => (who === 'shown' ? shownPeople.filter((p) => !excluded.has(p.id)) : allPeople),
    [who, shownPeople, allPeople, excluded],
  );
  // The shown people as the picker lists them: name, then department and location.
  const pickList = useMemo(
    () => shownPeople.map((p) => ({ key: p.id, name: p.name, detail: [p.department, p.location].filter(Boolean).join(' · ') })),
    [shownPeople],
  );
  // The chosen columns in canonical order, name always first-in.
  const chosen = useMemo(() => {
    const set = new Set([LOCKED, ...columns]);
    return COLUMNS.filter((c) => set.has(c.key));
  }, [columns]);

  /** Toggle one column on or off (name cannot be turned off). */
  const toggleColumn = (key: string) => {
    if (key === LOCKED) return;
    setColumns((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  };

  const sheets = ['Staff', ...(includeSummary ? ['Summary by department'] : []), ...(includeHistory ? ['Change history'] : [])];
  const previewCols = chosen.slice(0, PREVIEW_COLS);
  const hiddenCount = chosen.length - previewCols.length;

  // When a sheet is unticked while its tab is open, fall back to the Staff tab so
  // the preview never points at a sheet that is no longer in the file.
  useEffect(() => {
    if (!sheets.includes(tab)) setTab('Staff');
  }, [includeSummary, includeHistory]);   // eslint-disable-line react-hooks/exhaustive-deps

  /** Build and download the workbook from the backend. */
  const download = async () => {
    setDownloading(true);
    try {
      const res = await exportStaff({
        employee_ids: people.map((p) => p.id),
        columns: chosen.map((c) => c.key),
        include_summary: includeSummary,
        include_history: includeHistory,
      });
      const url = URL.createObjectURL(res.data);              // the .xlsx blob
      const a = document.createElement('a');
      a.href = url;
      a.download = `staff-directory-${today()}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
      onNotify('Staff export downloaded.', 'success');
      onClose();
    } catch {
      onNotify('The export could not be built.', 'error');
    } finally {
      setDownloading(false);
    }
  };

  // Per-department summary for the preview (computed from the list people).
  const summaryRows = useMemo(() => {
    const by: Record<string, ExportPerson[]> = {};
    people.forEach((p) => { (by[p.department || 'Not set'] = by[p.department || 'Not set'] || []).push(p); });
    return Object.keys(by).sort().map((dept) => {
      const g = by[dept];
      const vac = g.reduce((s, p) => s + (p.balances?.vacation_balance ?? 0), 0);
      const below = g.filter((p) => ['vacation_balance', 'sick_balance', 'overtime', 'carryover'].some((k) => (p.balances?.[k] ?? 0) < 0)).length;
      return { dept, people: g.length, vac: previewNum(vac), avg: previewNum(vac / g.length), below };
    });
  }, [people]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle>
        Export to Excel
        <Typography variant="body2" color="text.secondary">
          staff-directory-{today()}.xlsx · {people.length} {people.length === 1 ? 'person' : 'people'} · {chosen.length} columns · {sheets.length} {sheets.length === 1 ? 'sheet' : 'sheets'}
        </Typography>
      </DialogTitle>
      <DialogContent dividers sx={{ p: 0 }}>
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: '300px minmax(0, 1fr)' } }}>
          {/* Options */}
          <Box sx={{ p: 2.5, borderRight: { md: 1 }, borderBottom: { xs: 1, md: 0 }, borderColor: 'divider', display: 'grid', gap: 1.5, alignContent: 'start' }}>
            <Typography variant="overline" color="text.secondary">People</Typography>
            <ToggleButtonGroup exclusive size="small" value={who} onChange={(_, v) => v && setWho(v)} fullWidth>
              <ToggleButton value="shown">The {shownPeople.length} shown</ToggleButton>
              <ToggleButton value="all">Everyone ({allPeople.length})</ToggleButton>
            </ToggleButtonGroup>
            {/* Untick anyone the search brought in by accident (#489). */}
            {who === 'shown' && <ExportPeoplePicker people={pickList} excluded={excluded} onChange={setExcluded} />}

            <Typography variant="overline" color="text.secondary">Columns</Typography>
            <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap">
              {PRESETS.map(([name, keys]) => {
                const on = keys.length === columns.length && keys.every((k) => columns.includes(k));
                return (
                  <Button key={name} size="small" variant={on ? 'contained' : 'outlined'} onClick={() => setColumns(keys.slice())}>{name}</Button>
                );
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

            <Typography variant="overline" color="text.secondary">Extra sheets</Typography>
            <FormControlLabel control={<Checkbox size="small" checked={includeSummary} onChange={(e) => setIncludeSummary(e.target.checked)} />} label={<Typography variant="body2">Summary by department</Typography>} />
            <FormControlLabel control={<Checkbox size="small" checked={includeHistory} onChange={(e) => setIncludeHistory(e.target.checked)} />} label={<Typography variant="body2">Change history</Typography>} />
          </Box>

          {/* Preview */}
          <Box sx={{ p: 2.5, minWidth: 0 }}>
            <Typography variant="overline" color="text.secondary">Preview</Typography>
            {sheets.length > 1 && (
              <Stack direction="row" spacing={0.5} sx={{ my: 1 }} useFlexGap flexWrap="wrap">
                {sheets.map((s) => (
                  <Button key={s} size="small" variant={tab === s ? 'contained' : 'text'} onClick={() => setTab(s)}>{s}</Button>
                ))}
              </Stack>
            )}

            <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 2, overflow: 'hidden' }}>
              <Box component="table" sx={{ width: '100%', tableLayout: 'fixed', borderCollapse: 'collapse', fontSize: 12.5, '& th, & td': { border: 1, borderColor: 'divider', p: '4px 8px', textAlign: 'left', verticalAlign: 'top', overflowWrap: 'anywhere', whiteSpace: 'normal' } }}>
                {(tab === 'Summary by department') ? (
                  <>
                    <Box component="thead"><Box component="tr" sx={{ bgcolor: 'action.hover' }}>
                      {['Department', 'People', 'Vacation total', 'Vacation average', 'Below zero'].map((h) => <Box component="th" key={h}>{h}</Box>)}
                    </Box></Box>
                    <Box component="tbody">
                      {summaryRows.slice(0, PREVIEW_ROWS).map((r) => (
                        <Box component="tr" key={r.dept}>
                          <Box component="td">{r.dept}</Box><Box component="td">{r.people}</Box>
                          <Box component="td">{r.vac}</Box><Box component="td">{r.avg}</Box><Box component="td">{r.below}</Box>
                        </Box>
                      ))}
                    </Box>
                  </>
                ) : (tab === 'Change history') ? (
                  <Box component="tbody"><Box component="tr"><Box component="td">
                    <Typography variant="body2" color="text.secondary">The change-history sheet is built from the saved edits of the chosen people when you download.</Typography>
                  </Box></Box></Box>
                ) : (
                  <>
                    <Box component="thead"><Box component="tr" sx={{ bgcolor: 'action.hover' }}>
                      {previewCols.map((c) => <Box component="th" key={c.key}>{c.label}</Box>)}
                    </Box></Box>
                    <Box component="tbody">
                      {people.slice(0, PREVIEW_ROWS).map((p) => (
                        <Box component="tr" key={p.id}>
                          {previewCols.map((c) => {
                            const v = c.get(p);
                            const negative = c.number && typeof v === 'number' && v < 0;
                            return (
                              <Box component="td" key={c.key} sx={{ textAlign: c.number ? 'right' : 'left', color: negative ? 'error.main' : undefined, fontVariantNumeric: c.number ? 'tabular-nums' : undefined }}>
                                {c.number && typeof v === 'number' ? previewNum(v) : v}
                              </Box>
                            );
                          })}
                        </Box>
                      ))}
                    </Box>
                  </>
                )}
              </Box>
            </Box>

            <Stack spacing={0.25} sx={{ mt: 1 }}>
              {tab === 'Staff' && hiddenCount > 0 && (
                <Typography variant="caption" color="text.secondary">and {hiddenCount} more column{hiddenCount === 1 ? '' : 's'} in the file: {chosen.slice(PREVIEW_COLS).map((c) => c.label).join(', ')}</Typography>
              )}
              {people.length > PREVIEW_ROWS && (
                <Typography variant="caption" color="text.secondary">Showing the first {PREVIEW_ROWS} of {people.length} rows.</Typography>
              )}
              <Typography variant="caption" color="text.secondary">Header frozen with Excel filters; balances are numbers with a totals row; below zero in red.</Typography>
            </Stack>
          </Box>
        </Box>
      </DialogContent>
      <Divider />
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={download} disabled={downloading || people.length === 0}>
          {downloading ? 'Building...' : 'Download .xlsx'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
