import { ReactNode, useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box, Button, Chip, Collapse, IconButton, Paper, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Typography, useMediaQuery,
} from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { useSearchParams } from 'react-router-dom';
import CloseIcon from '@mui/icons-material/Close';
import FilterListIcon from '@mui/icons-material/FilterList';
import DownloadIcon from '@mui/icons-material/Download';
import { EmployeeSetupSummary } from '../EmployeeSetupList';
import { ManagerOption } from '../AddEmployee';
import { getManagerAssignments, getSpUsers } from '../../api/client';
import { getStaffRecord, StaffRecord, getArchivedStaff, restoreStaff, ArchivedPerson } from '../../api/staff';
import { isHourly } from '../../utils/employment';
import { formatInstantDay } from '../../utils/dates';
import { Enter } from '../Motion';
import PersonProfile from './PersonProfile';
import StaffExportDialog, { ExportPerson } from './StaffExportDialog';

/**
 * A name in the form two spellings of it are compared in: trimmed, spaces
 * squeezed to one, lower case.
 *
 * @param name - A staff or request name; blank when missing.
 * @returns The comparison key.
 */
function nameKey(name: unknown): string {
  return String(name ?? '').replace(/\s+/g, ' ').trim().toLowerCase();  // same rule as the server's name match
}

/** One person as /admin/balances returns them, plus their managers once loaded. */
interface Person {
  id: string;
  name: string;
  email: string;
  department: string;
  location: string;
  employment_type: string;
  system_check: string;
  is_manager: boolean;
  balances: Record<string, number>;
  /** Manager names, or null until /admin/manager-assignments has answered. */
  managers: string[] | null;
}

/** A person is below zero in any pot that is a floor for them. Hourly staff have
 *  only Sick; everyone else can go negative on vacation too (#300). */
const isBelowZero = (p: Person) =>
  isHourly(p.balances)
    ? (p.balances?.sick_balance ?? 0) < 0
    : ['vacation_balance', 'sick_balance', 'overtime', 'carryover'].some((k) => (p.balances?.[k] ?? 0) < 0);

/** One filter group: its key, label, the values on offer, and its test. */
interface FilterGroup {
  key: string;
  label: string;
  opts: (people: Person[]) => string[];
  test: (p: Person, value: string, problems: Record<string, string[]>) => boolean;
  /** "Needs attention" must match every chosen value, not any. */
  all?: boolean;
}

/** Unique, sorted, non-blank values from a list. */
const uniq = (xs: (string | null | undefined)[]) =>
  Array.from(new Set(xs.filter((x): x is string => !!x))).sort();

/** The six filter groups, mirroring the mockup's drawer. */
const FILTERS: FilterGroup[] = [
  { key: 'location', label: 'Location', opts: (ps) => uniq(ps.map((p) => p.location)), test: (p, v) => p.location === v },
  { key: 'department', label: 'Department', opts: (ps) => uniq(ps.map((p) => p.department)), test: (p, v) => p.department === v },
  { key: 'employment_type', label: 'Salary or hourly', opts: (ps) => uniq(ps.map((p) => p.employment_type)), test: (p, v) => p.employment_type === v },
  { key: 'manager', label: 'Manager', opts: (ps) => uniq(ps.flatMap((p) => p.managers || [])), test: (p, v) => (p.managers || []).includes(v) },
  { key: 'system_check', label: 'System check', opts: (ps) => uniq(ps.map((p) => p.system_check)), test: (p, v) => p.system_check === v },
  {
    key: 'flag', label: 'Needs attention', all: true,
    opts: () => ['Below zero', 'No manager', 'Setup problem'],
    test: (p, v, problems) =>
      v === 'Below zero' ? isBelowZero(p)
        : v === 'No manager' ? p.managers !== null && p.managers.length === 0
          : !!problems[p.id],
  },
];

/** The quick chips above the table: each toggles one "Needs attention" value. */
const QUICK: [string, string | null][] = [
  ['All', null], ['Below zero', 'Below zero'], ['No manager', 'No manager'], ['Setup problem', 'Setup problem'],
];

interface Props {
  /** Everyone, from /admin/balances. */
  employees: any[];
  /** Every request, from /admin/requests, for the person panel's list. */
  requests: any[];
  /** The setup check, so a person with a problem is flagged. */
  setup: EmployeeSetupSummary | null;
  /** Whether writes are enabled (the profile's Save is off when they are not). */
  processingEnabled: boolean;
  /** Open the manager assignments screen. */
  onManageAssignments: () => void;
  /** Re-fetch the dashboard's balances and requests after a save, so the table
   *  row follows the change (the profile reloads its own record separately). */
  onReload?: () => void;
  /** Surface a message to the dashboard snackbar. */
  onNotify: (message: string, severity: 'success' | 'error') => void;
}

/**
 * Staff: the simple table of everyone with their balances and managers, with a
 * quick-filter row, a filter drawer and an Export button. Clicking a person
 * grows the table into their profile in place (balances, every Staff Directory
 * detail, managers and change history, saved with a required reason).
 *
 * @param props - See {@link Props}.
 * @returns The staff screen body.
 */
export default function StaffScreen({ employees, requests, setup, processingEnabled, onManageAssignments, onReload, onNotify }: Props) {
  const theme = useTheme();
  const [searchParams] = useSearchParams();                             // ?open=<id> from a deep link
  const [query, setQuery] = useState('');
  const [picked, setPicked] = useState<Record<string, string[]>>({});   // filter key -> chosen values
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);            // the person whose profile is open
  const [record, setRecord] = useState<StaffRecord | null>(null);       // their fetched record
  const [recordLoading, setRecordLoading] = useState(false);
  const [spUsers, setSpUsers] = useState<ManagerOption[]>([]);          // people for the pickers
  const [archived, setArchived] = useState<ArchivedPerson[]>([]);       // departed people (#119)
  const [archivedOpen, setArchivedOpen] = useState(false);             // the archived list, collapsed by default
  const [restoringId, setRestoringId] = useState<string | null>(null);  // a restore in flight

  // The archived people, for the collapsible list at the foot of the screen.
  const loadArchived = useCallback(() => {
    getArchivedStaff()
      .then((r) => setArchived(r.data.archived || []))
      .catch(() => setArchived([]));                        // the list just stays empty
  }, []);
  useEffect(() => { loadArchived(); }, [loadArchived]);

  /** Restore one archived person, then refresh the archived list and the table. */
  const doRestore = async (id: string) => {
    setRestoringId(id);
    try {
      await restoreStaff(id);
      onNotify('The person was restored.', 'success');
      loadArchived();                                        // drop them from the archived list
      onReload?.();                                          // bring them back into the staff table
      loadManagers();                                        // their managers column follows
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      onNotify(typeof detail === 'string' ? detail : 'The person could not be restored.', 'error');
    } finally {
      setRestoringId(null);
    }
  };

  // Manager names by employee id; /admin/balances does not carry them.
  const [managersById, setManagersById] = useState<Record<string, string[]> | null>(null);
  const loadManagers = useCallback(() => {
    getManagerAssignments()
      .then((r) => setManagersById(Object.fromEntries(
        (r.data.assignments || []).map((a: any) => [String(a.id), (a.managers || []).map((m: any) => m.name).filter(Boolean)]),
      )))
      .catch(() => setManagersById(null));                  // the column just shows nothing
  }, []);
  useEffect(() => { loadManagers(); }, [loadManagers]);

  // People for the profile's person/manager pickers; fetched once when the screen opens.
  useEffect(() => {
    getSpUsers()
      .then((r) => setSpUsers((r.data.users || []).map((u: any) => ({ sp_user_id: u.sp_user_id, name: u.name }))))
      .catch(() => { /* the pickers are just empty */ });
  }, []);

  // Setup problems by employee id, for the flag on each row and the filter.
  const problemsById = useMemo(() => Object.fromEntries(
    (setup?.flagged || []).map((f) => [String(f.employee_id), f.fails.map((p) => p.detail || p.code)]),
  ) as Record<string, string[]>, [setup]);

  const people = useMemo<Person[]>(() => employees.map((e) => ({
    ...e,
    id: String(e.id),
    employment_type: e.employment_type || '',
    system_check: e.system_check || '',
    managers: managersById ? managersById[String(e.id)] ?? [] : null,
  })).sort((a, b) => a.name.localeCompare(b.name)), [employees, managersById]);

  /** Whether a person passes the search box and every active filter. */
  const passes = (p: Person): boolean => {
    const q = query.trim().toLowerCase();
    if (q && !`${p.name} ${p.email} ${(p.managers || []).join(' ')} ${p.department} ${p.location}`.toLowerCase().includes(q)) return false;
    return FILTERS.every((f) => {
      const vals = picked[f.key] || [];
      if (!vals.length) return true;                       // no values chosen -> no constraint
      return f.all ? vals.every((v) => f.test(p, v, problemsById)) : vals.some((v) => f.test(p, v, problemsById));
    });
  };
  const rows = useMemo(() => people.filter(passes), [people, query, picked, problemsById]);  // eslint-disable-line react-hooks/exhaustive-deps
  // Salaried and hourly shown in two tables with columns that fit each (#338).
  const salariedRows = useMemo(() => rows.filter((p) => !isHourly(p.employment_type)), [rows]);
  const hourlyRows = useMemo(() => rows.filter((p) => isHourly(p.employment_type)), [rows]);

  /** Toggle one value within a filter group. */
  const toggleFilter = (key: string, value: string) => {
    setPicked((prev) => {
      const vals = prev[key] || [];
      const next = vals.includes(value) ? vals.filter((v) => v !== value) : [...vals, value];
      return { ...prev, [key]: next };
    });
  };

  const activeChips = FILTERS.flatMap((f) => (picked[f.key] || []).map((v) => ({ key: f.key, label: f.label, value: v })));
  const filterCount = activeChips.length;

  /** Open a person: fetch their record, then show the profile in place. */
  const openPerson = async (id: string) => {
    setOpenId(id);
    setRecord(null);
    setRecordLoading(true);
    try {
      const res = await getStaffRecord(id);
      setRecord(res.data);
    } catch {
      onNotify('That person could not be loaded.', 'error');
      setOpenId(null);
    } finally {
      setRecordLoading(false);
    }
  };

  // Opened from a deep link with ?open=<id>: show that person's
  // profile at once. Runs when the param changes, so closing the profile by hand
  // (the param stays) does not spring it back open.
  useEffect(() => {
    const id = searchParams.get('open');
    if (id && id !== openId) openPerson(id);
  }, [searchParams]);  // eslint-disable-line react-hooks/exhaustive-deps

  /** Reload everything a save can touch: the dashboard balances/requests, the
   *  manager assignments (for the row), and the open person's full record. */
  const reloadRecord = async () => {
    onReload?.();                                           // dashboard balances + requests
    loadManagers();                                        // the row's managers column
    loadArchived();                                        // a person just archived appears in the list (#119)
    if (!openId) return;
    try {
      const res = await getStaffRecord(openId);            // the profile's own record
      setRecord(res.data);
    } catch {
      /* the save already succeeded; the view just keeps the pre-save figures */
    }
  };

  const openPersonSummary = people.find((p) => p.id === openId) || null;
  // Match the person's requests by name ignoring case and spacing, so a stored
  // name with a stray space ("Glenn Clarke ") still finds its requests.
  const theirKey = openPersonSummary ? nameKey(openPersonSummary.name) : '';
  const theirRequests = openPersonSummary ? requests.filter((r) => nameKey(r.employee_name) === theirKey) : [];

  // The person's record follows the live-balance row while the full record loads.
  if (openId && record) {
    return (
      <Enter>
        <PersonProfile
          record={record}
          requests={theirRequests}
          users={spUsers}
          processingEnabled={processingEnabled}
          onBack={() => { setOpenId(null); setRecord(null); }}
          onSaved={reloadRecord}
          onNotify={onNotify}
        />
      </Enter>
    );
  }
  if (openId && recordLoading) {
    return <Paper sx={{ p: 4 }}><Typography color="text.secondary">Loading the person...</Typography></Paper>;
  }

  return (
    <>
      <Box sx={{ display: 'flex', gap: 2, alignItems: 'center', flexWrap: 'wrap', mt: -1.5 }}>
        <Typography color="text.secondary" sx={{ flex: 1, minWidth: 240 }}>
          Everyone with their balances and managers. Click a person to see and edit their whole record.
        </Typography>
        <Button variant="outlined" onClick={onManageAssignments}>Manage assignments</Button>
      </Box>

      <Paper sx={{ overflow: 'hidden' }}>
        {/* Toolbar: search, quick chips, filters toggle, export. */}
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', p: 1.5, borderBottom: 1, borderColor: 'divider' }}>
          <TextField
            size="small" value={query} placeholder="Search name, email, manager, department or location"
            onChange={(e) => setQuery(e.target.value)} inputProps={{ 'aria-label': 'Search staff' }}
            sx={{ flex: { xs: '1 1 100%', sm: '1 1 240px' }, minWidth: 0 }}
          />
          {QUICK.map(([label, value]) => {
            const flags = picked.flag || [];
            const none = !Object.values(picked).some((v) => v.length);
            const on = value ? flags.includes(value) : none;
            const count = value ? people.filter((p) => FILTERS[5].test(p, value, problemsById)).length : people.length;
            return (
              <Chip
                key={label} label={`${label} ${count}`} aria-pressed={on}
                color={on ? 'primary' : 'default'} variant={on ? 'filled' : 'outlined'}
                onClick={() => { if (!value) setPicked({}); else toggleFilter('flag', value); }}
              />
            );
          })}
          <Button
            variant={filtersOpen ? 'contained' : 'outlined'} startIcon={<FilterListIcon />}
            onClick={() => setFiltersOpen((o) => !o)} aria-expanded={filtersOpen}
          >
            Filters{filterCount ? ` (${filterCount})` : ''}
          </Button>
          <Button variant="outlined" startIcon={<DownloadIcon />} onClick={() => setExportOpen(true)}>Export to Excel</Button>
        </Box>

        {/* Filter drawer: one group per filter, a value toggles on click. */}
        <Collapse in={filtersOpen}>
          <Box sx={{ p: 2, display: 'grid', gap: 1.5, bgcolor: 'action.hover', borderBottom: 1, borderColor: 'divider' }}>
            {FILTERS.map((f) => (
              <Box key={f.key} sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '140px minmax(0, 1fr)' }, gap: 1, alignItems: 'start' }}>
                <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary', pt: 0.75 }}>{f.label}</Typography>
                <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap">
                  {f.opts(people).map((v) => {
                    const on = (picked[f.key] || []).includes(v);
                    return <Chip key={v} size="small" label={v} color={on ? 'primary' : 'default'} variant={on ? 'filled' : 'outlined'} onClick={() => toggleFilter(f.key, v)} aria-pressed={on} />;
                  })}
                </Stack>
              </Box>
            ))}
          </Box>
        </Collapse>

        {/* Active filter chips, removable. */}
        {filterCount > 0 && (
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, alignItems: 'center', p: 1.5, borderBottom: 1, borderColor: 'divider' }}>
            {activeChips.map((c) => (
              <Chip key={`${c.key}-${c.value}`} size="small" color="primary" variant="outlined" label={`${c.label}: ${c.value}`} onDelete={() => toggleFilter(c.key, c.value)} deleteIcon={<CloseIcon />} />
            ))}
            <Button size="small" onClick={() => setPicked({})}>Clear all</Button>
          </Box>
        )}

        {rows.length === 0 ? (
          <Typography color="text.secondary" sx={{ p: 3 }}>{people.length ? 'Nobody matches these filters.' : 'No staff to show.'}</Typography>
        ) : (
          <>
            {/* Salaried: every pot, as before. */}
            <StaffGroup title="Salaried" count={salariedRows.length}>
              {salariedRows.length
                ? <StaffTable variant="salaried" people={people} rows={salariedRows} problemsById={problemsById} onOpen={openPerson} theme={theme} />
                : <Typography color="text.secondary" sx={{ p: 2 }}>No salaried staff match.</Typography>}
            </StaffGroup>
            {/* Hourly: Sick plus the vacation-taken counter; no Make-up, Carry-over or Payout (#300). */}
            <StaffGroup title="Hourly" count={hourlyRows.length}>
              {hourlyRows.length
                ? <StaffTable variant="hourly" people={people} rows={hourlyRows} problemsById={problemsById} onOpen={openPerson} theme={theme} />
                : <Typography color="text.secondary" sx={{ p: 2 }}>No hourly staff match.</Typography>}
            </StaffGroup>
          </>
        )}

        <Box sx={{ p: 1.5, borderTop: 1, borderColor: 'divider' }}>
          <Typography variant="body2" color="text.secondary">Showing {rows.length} of {people.length} people ({salariedRows.length} salaried, {hourlyRows.length} hourly)</Typography>
        </Box>
      </Paper>

      {/* Archived people (#119): departed staff, out of the tables above. Collapsed
          by default; Restore brings one back into the Staff Directory. */}
      <Paper sx={{ overflow: 'hidden' }}>
        <Box
          role="button" tabIndex={0} aria-expanded={archivedOpen}
          onClick={() => setArchivedOpen((o) => !o)}
          onKeyDown={(e) => { if (e.key === 'Enter') setArchivedOpen((o) => !o); }}
          sx={{ display: 'flex', alignItems: 'center', gap: 1, p: 1.75, cursor: 'pointer', '&:hover': { bgcolor: 'action.hover' } }}
        >
          <Typography sx={{ fontWeight: 700 }}>Archived</Typography>
          <Chip size="small" label={archived.length} />
          <Box sx={{ flex: 1 }} />
          <Typography variant="caption" color="text.secondary">{archivedOpen ? 'Hide' : 'Show'}</Typography>
        </Box>
        <Collapse in={archivedOpen}>
          {archived.length === 0 ? (
            <Typography color="text.secondary" sx={{ p: 2, borderTop: 1, borderColor: 'divider' }}>Nobody is archived.</Typography>
          ) : (
            <Table size="small" sx={{ tableLayout: 'fixed', borderTop: 1, borderColor: 'divider', '& td, & th': { borderColor: 'divider', overflowWrap: 'anywhere', whiteSpace: 'normal' } }}>
              <TableHead>
                <TableRow>
                  <TableCell>Employee</TableCell>
                  <TableCell>Archived</TableCell>
                  <TableCell>Reason</TableCell>
                  <TableCell align="right">Action</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {archived.map((a) => (
                  <TableRow key={a.id}>
                    <TableCell>
                      <Typography sx={{ fontWeight: 600 }}>{a.name}</Typography>
                      <Typography variant="caption" color="text.secondary">{a.department || 'No department'} · {a.location || 'No location'}</Typography>
                    </TableCell>
                    {/* The archive date read as "Nov 2, 2026". */}
                    <TableCell>{a.archived_at ? formatInstantDay(a.archived_at) : ''}</TableCell>
                    <TableCell>{a.archived_reason || ''}</TableCell>
                    <TableCell align="right">
                      <Button
                        size="small" variant="outlined"
                        onClick={() => doRestore(a.id)} disabled={restoringId === a.id}
                      >
                        {restoringId === a.id ? 'Restoring...' : 'Restore'}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Collapse>
      </Paper>

      <StaffExportDialog
        open={exportOpen}
        shownPeople={rows as unknown as ExportPerson[]}
        allPeople={people as unknown as ExportPerson[]}
        onClose={() => setExportOpen(false)}
        onNotify={onNotify}
      />
    </>
  );
}

/** A balance figure: two decimals, below zero in the error colour. */
function balance(v: number | undefined) {
  const n = v ?? 0;
  return { text: String(Math.round(n * 100) / 100), negative: n < 0 };
}

/**
 * A labelled section above one staff table, naming the group and its size.
 *
 * @param props.title - "Salaried" or "Hourly".
 * @param props.count - How many people are in the group.
 * @param props.children - The table, or a short empty line.
 * @returns The section.
 */
function StaffGroup({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return (
    <Box>
      {/* A plain heading bar so the two groups read as separate tables. */}
      <Box sx={{ px: 1.75, py: 1, display: 'flex', alignItems: 'baseline', gap: 1, bgcolor: 'action.hover', borderBottom: 1, borderColor: 'divider' }}>
        <Typography sx={{ fontWeight: 700, fontSize: 14 }}>{title}</Typography>
        <Typography variant="caption" color="text.secondary">{count} {count === 1 ? 'person' : 'people'}</Typography>
      </Box>
      {children}
    </Box>
  );
}

/** A numeric balance cell, below zero in the error colour and bold. */
function BalanceCell({ value }: { value: number | undefined }) {
  const b = balance(value);
  return (
    <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums', color: b.negative ? 'error.main' : undefined, fontWeight: b.negative ? 700 : undefined }}>
      {b.text}
    </TableCell>
  );
}

/**
 * One staff table that never scrolls sideways: cells wrap, the least important
 * columns drop by width (Managers, then Carry-over and Make-up for salaried), a
 * footer names the hidden columns, and below ~560px rows become cards. Every
 * hidden value stays reachable by opening the person. The salaried variant
 * shows every pot as before; the hourly variant shows Sick and the
 * vacation-taken counter only, with no Make-up, Carry-over or Payout (#300, #338).
 *
 * @param props.people - Everyone (for the empty-state copy).
 * @param props.rows - The filtered rows of this group to show.
 * @param props.problemsById - Setup problems per id, for the row flag.
 * @param props.onOpen - Open a person's profile.
 * @param props.theme - The MUI theme (for tokens).
 * @param props.variant - "salaried" (every pot) or "hourly" (sick plus counter).
 * @returns The table or cards.
 */
function StaffTable({ people, rows, problemsById, onOpen, variant }: {
  people: Person[]; rows: Person[]; problemsById: Record<string, string[]>; onOpen: (id: string) => void; theme: ReturnType<typeof useTheme>; variant: 'salaried' | 'hourly';
}) {
  const wide = useMediaQuery('(min-width:900px)');          // Managers column fits
  const mid = useMediaQuery('(min-width:720px)');           // Make-up and Carry-over fit
  const cards = useMediaQuery('(max-width:560px)');         // below this, rows become cards
  const hourly = variant === 'hourly';                     // sick plus the vacation-taken counter only

  // Which columns are visible; hourly never has Make-up or Carry-over to drop.
  const showManagers = wide;
  const showMakeUp = mid && !hourly;
  const showCarryOver = mid && !hourly;
  const hidden = [
    !showManagers && 'Managers',
    !hourly && !showMakeUp && 'Make-up',
    !hourly && !showCarryOver && 'Carry-over',
  ].filter(Boolean) as string[];

  if (rows.length === 0) {
    return <Typography color="text.secondary" sx={{ p: 3 }}>{people.length ? 'Nobody matches these filters.' : 'No staff to show.'}</Typography>;
  }

  // Below ~560px: labelled cards, so nothing has to scroll sideways.
  if (cards) {
    // The balance lines each card shows, by variant.
    const salaryPairs: [string, string][] = [['Vacation', 'vacation_balance'], ['Sick', 'sick_balance'], ['Make-up', 'overtime'], ['Carry-over', 'carryover']];
    return (
      <Stack divider={<Box sx={{ borderTop: 1, borderColor: 'divider' }} />}>
        {rows.map((p) => (
          <Box key={p.id} role="button" tabIndex={0} onClick={() => onOpen(p.id)} onKeyDown={(e) => { if (e.key === 'Enter') onOpen(p.id); }} sx={{ p: 2, cursor: 'pointer', '&:hover': { bgcolor: 'action.hover' } }}>
            <Typography sx={{ fontWeight: 700 }}>
              {p.name}
              {problemsById[p.id] && <Chip size="small" color="error" variant="outlined" label="Setup problem" sx={{ ml: 1 }} />}
            </Typography>
            <Typography variant="caption" color="text.secondary">{p.department || 'No department'} · {p.location || 'No location'}</Typography>
            <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 0.5, mt: 1 }}>
              {hourly ? (
                <>
                  {/* Vacation is a counter here, not a balance; Sick is the only pot. */}
                  <Typography variant="body2" title="Vacation taken this year. Counted, not a balance.">
                    <Box component="span" color="text.secondary">Vacation taken: </Box><b style={{ fontVariantNumeric: 'tabular-nums' }}>{balance(p.balances?.vacation_taken).text}</b>
                  </Typography>
                  {(() => {
                    const b = balance(p.balances?.sick_balance);
                    return (
                      <Typography variant="body2" sx={{ color: b.negative ? 'error.main' : undefined }}>
                        <Box component="span" color="text.secondary">Sick: </Box><b style={{ fontVariantNumeric: 'tabular-nums' }}>{b.text}</b>
                      </Typography>
                    );
                  })()}
                </>
              ) : (
                salaryPairs.map(([label, k]) => {
                  const b = balance(p.balances?.[k]);
                  return (
                    <Typography key={k} variant="body2" sx={{ color: b.negative ? 'error.main' : undefined }}>
                      <Box component="span" color="text.secondary">{label}: </Box><b style={{ fontVariantNumeric: 'tabular-nums' }}>{b.text}</b>
                    </Typography>
                  );
                })
              )}
              <Typography variant="body2" sx={{ gridColumn: '1 / -1' }}>
                <Box component="span" color="text.secondary">Managers: </Box>
                {p.managers === null ? '' : p.managers.length ? p.managers.join(', ') : 'None'}
              </Typography>
            </Box>
          </Box>
        ))}
      </Stack>
    );
  }

  return (
    <>
      <Table size="small" sx={{ tableLayout: 'fixed', '& td, & th': { borderColor: 'divider', overflowWrap: 'anywhere', whiteSpace: 'normal' } }}>
        <TableHead>
          <TableRow>
            <TableCell>Employee</TableCell>
            {showManagers && <TableCell>Managers</TableCell>}
            {hourly ? (
              <>
                <TableCell align="right">Sick</TableCell>
                <TableCell align="right">Vacation taken</TableCell>
              </>
            ) : (
              <>
                <TableCell align="right">Vacation</TableCell>
                <TableCell align="right">Sick</TableCell>
                {showMakeUp && <TableCell align="right">Make-up</TableCell>}
                {showCarryOver && <TableCell align="right">Carry-over</TableCell>}
              </>
            )}
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((p) => (
            <TableRow key={p.id} hover tabIndex={0} sx={{ cursor: 'pointer' }} onClick={() => onOpen(p.id)} onKeyDown={(e) => { if (e.key === 'Enter') onOpen(p.id); }}>
              <TableCell>
                <Typography sx={{ fontWeight: 600 }}>
                  {p.name}
                  {problemsById[p.id] && <Chip size="small" color="error" variant="outlined" label="Setup problem" sx={{ ml: 1 }} />}
                </Typography>
                <Typography variant="caption" color="text.secondary">{p.department || 'No department'} · {p.location || 'No location'}</Typography>
              </TableCell>
              {showManagers && (
                <TableCell>
                  {p.managers === null ? '' : p.managers.length ? p.managers.join(', ') : <Chip size="small" color="error" variant="outlined" label="None" />}
                </TableCell>
              )}
              {hourly ? (
                <>
                  <BalanceCell value={p.balances?.sick_balance} />
                  {/* Vacation is a counter ("N taken"), not a spendable balance. */}
                  <TableCell align="right" title="Vacation taken this year. Counted, not a balance." sx={{ fontVariantNumeric: 'tabular-nums', color: 'text.secondary' }}>
                    {balance(p.balances?.vacation_taken).text} taken
                  </TableCell>
                </>
              ) : (
                <>
                  <BalanceCell value={p.balances?.vacation_balance} />
                  <BalanceCell value={p.balances?.sick_balance} />
                  {showMakeUp && <BalanceCell value={p.balances?.overtime} />}
                  {showCarryOver && <BalanceCell value={p.balances?.carryover} />}
                </>
              )}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {hidden.length > 0 && (
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', px: 2, pt: 1 }}>
          Hidden at this width: {hidden.join(', ')}. Open a person to see every value.
        </Typography>
      )}
    </>
  );
}
