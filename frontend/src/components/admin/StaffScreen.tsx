import { useEffect, useMemo, useState } from 'react';
import {
  Box, Button, Chip, Drawer, IconButton, MenuItem, Paper, Stack, Tab, Table, TableBody, TableCell,
  TableHead, TableRow, Tabs, TextField, Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import KindPill from '../KindPill';
import { STATUS_COLOR } from '../dataGridDefaults';
import { EmployeeSetupSummary } from '../EmployeeSetupList';
import { getManagerAssignments } from '../../api/client';
import { requestKind, requestWhat, requestWhen } from '../../utils/requestText';

/** One person as /admin/balances returns them, plus their managers once loaded. */
interface Person {
  id: string;
  name: string;
  email: string;
  department: string;
  location: string;
  is_manager: boolean;
  balances: Record<string, number>;
  /** Manager names, or null until /admin/manager-assignments has answered. */
  managers: string[] | null;
}

/** The quick filters and what each keeps. */
const FILTERS: Record<string, (p: Person) => boolean> = {
  All: () => true,
  'Below zero': (p) => (p.balances?.vacation_balance ?? 0) < 0 || (p.balances?.sick_balance ?? 0) < 0,
  'No manager': (p) => p.managers !== null && p.managers.length === 0,
  Managers: (p) => p.is_manager,
};

/** Grouping options and the field each groups by. */
const GROUPS: Record<string, keyof Person | null> = {
  'No grouping': null,
  'By department': 'department',
  'By location': 'location',
};

interface Props {
  /** Everyone, from /admin/balances. */
  employees: any[];
  /** Every request, from /admin/requests, for the person panel's list. */
  requests: any[];
  /** The setup check, so a person with a problem is flagged. */
  setup: EmployeeSetupSummary | null;
  /** Open someone's dashboard in a new tab. */
  onOpenDashboard: (id: string, role: 'employee' | 'manager') => void;
  /** Copy someone's employee dashboard link. */
  onCopyLink: (id: string) => void;
  /** Email a manager their dashboard link. */
  onSendLink: (id: string) => void;
  /** Open the manager assignments screen. */
  onManageAssignments: () => void;
}

/**
 * A balance cell: two decimals at most, below zero in the error colour.
 *
 * @param props.v - The balance in days.
 * @param props.narrowHide - Hide it on phones.
 * @returns The table cell.
 */
function Num({ v, narrowHide }: { v: number | undefined; narrowHide?: boolean }) {
  const n = v ?? 0;
  return (
    <TableCell
      align="right"
      sx={{
        fontVariantNumeric: 'tabular-nums',                    // digits line up down the column
        color: n < 0 ? 'error.main' : undefined, fontWeight: n < 0 ? 700 : undefined,
        display: narrowHide ? { xs: 'none', sm: 'table-cell' } : undefined,
      }}
    >
      {Math.round(n * 100) / 100}
    </TableCell>
  );
}

/**
 * Staff: everyone with their balances and managers in one table, replacing
 * All balances, Department summary, View employee and View team. Search,
 * quick filters and grouping (by department or location, with averages) sit
 * above it; clicking a person opens a panel with their requests, managers and
 * dashboard links.
 *
 * @param props - See {@link Props}.
 * @returns The staff screen body.
 */
export default function StaffScreen({ employees, requests, setup, onOpenDashboard, onCopyLink, onSendLink, onManageAssignments }: Props) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('All');
  const [group, setGroup] = useState('No grouping');
  const [openId, setOpenId] = useState<string | null>(null);
  const [panelTab, setPanelTab] = useState(0);
  // Manager names by employee id; /admin/balances does not carry them.
  const [managersById, setManagersById] = useState<Record<string, string[]> | null>(null);

  useEffect(() => {
    getManagerAssignments()
      .then((r) => setManagersById(Object.fromEntries(
        (r.data.assignments || []).map((a: any) => [String(a.id), (a.managers || []).map((m: any) => m.name).filter(Boolean)]),
      )))
      .catch(() => setManagersById(null));                  // the column just shows nothing
  }, []);

  // Setup problems by employee id, for the flag on each row.
  const problemsById = useMemo(() => Object.fromEntries(
    (setup?.flagged || []).map((f) => [String(f.employee_id), f.fails.map((p) => p.detail || p.code)]),
  ), [setup]);

  const people = useMemo<Person[]>(() => employees.map((e) => ({
    ...e,
    id: String(e.id),
    managers: managersById ? managersById[String(e.id)] ?? [] : null,
  })).sort((a, b) => a.name.localeCompare(b.name)), [employees, managersById]);

  // Filtered by the chip and the search box.
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return people.filter((p) => FILTERS[filter](p) && (!q || `${p.name} ${p.email} ${(p.managers || []).join(' ')}`.toLowerCase().includes(q)));
  }, [people, filter, query]);

  // Grouped rows with each group's averages, or one unnamed group.
  const groups = useMemo(() => {
    const key = GROUPS[group];
    if (!key) return [{ name: '', rows }];
    const by: Record<string, Person[]> = {};
    rows.forEach((p) => { const g = String(p[key] || 'Not set'); (by[g] = by[g] || []).push(p); });
    return Object.keys(by).sort().map((name) => ({ name, rows: by[name] }));
  }, [rows, group]);

  const person = people.find((p) => p.id === openId) || null;
  const theirs = person ? requests.filter((r) => r.employee_name === person.name) : [];

  /** Average of one balance over a group. @param ps - The group. @param k - Balance key. @returns One decimal. */
  const avg = (ps: Person[], k: string) => (ps.reduce((s, p) => s + (p.balances?.[k] ?? 0), 0) / ps.length).toFixed(1);

  return (
    <>
      <Box sx={{ display: 'flex', gap: 2, alignItems: 'center', flexWrap: 'wrap', mt: -1.5 }}>
        <Typography color="text.secondary" sx={{ flex: 1, minWidth: 240 }}>
          Everyone with their balances and managers. Click a person for their requests, managers and dashboard links.
        </Typography>
        <Button variant="outlined" onClick={onManageAssignments}>Manage assignments</Button>
      </Box>

      <Paper sx={{ overflow: 'hidden' }}>
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', p: 1.5, borderBottom: 1, borderColor: 'divider' }}>
          <TextField
            size="small" value={query} placeholder="Search name, email or manager"
            onChange={(e) => setQuery(e.target.value)}
            inputProps={{ 'aria-label': 'Search staff' }}
            sx={{ flex: { xs: '1 1 100%', sm: '0 1 260px' } }}
          />
          {Object.keys(FILTERS).map((f) => (
            <Chip
              key={f}
              label={`${f} ${people.filter(FILTERS[f]).length}`}
              onClick={() => setFilter(f)}
              aria-pressed={f === filter}
              color={f === filter ? 'primary' : 'default'}
              variant={f === filter ? 'filled' : 'outlined'}
            />
          ))}
          <TextField select size="small" label="Group" value={group} onChange={(e) => setGroup(e.target.value)} sx={{ minWidth: 160, ml: { md: 'auto' } }}>
            {Object.keys(GROUPS).map((g) => <MenuItem key={g} value={g}>{g}</MenuItem>)}
          </TextField>
        </Box>

        {rows.length === 0 ? (
          <Typography color="text.secondary" sx={{ p: 3 }}>Nobody matches. Clear the search or pick All.</Typography>
        ) : (
          <Box sx={{ overflowX: 'auto' }}>
            <Table size="small" sx={{ '& td, & th': { borderColor: 'divider' } }}>
              <TableHead>
                <TableRow>
                  <TableCell>Employee</TableCell>
                  <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>Managers</TableCell>
                  <TableCell align="right">Vacation</TableCell>
                  <TableCell align="right">Sick</TableCell>
                  <TableCell align="right" sx={{ display: { xs: 'none', sm: 'table-cell' } }}>Make-Up</TableCell>
                  <TableCell align="right" sx={{ display: { xs: 'none', sm: 'table-cell' } }}>Carry Over</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {groups.map((g) => [
                  // A group heading row with its size and averages (what Department summary showed).
                  g.name && (
                    <TableRow key={`g-${g.name}`}>
                      <TableCell colSpan={6} sx={{ bgcolor: 'action.hover' }}>
                        <b>{g.name}</b>{' '}
                        <Typography component="span" variant="body2" color="text.secondary">
                          {g.rows.length} people · avg vacation {avg(g.rows, 'vacation_balance')} · avg sick {avg(g.rows, 'sick_balance')}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ),
                  ...g.rows.map((p) => (
                    <TableRow
                      key={p.id} hover tabIndex={0} sx={{ cursor: 'pointer' }}
                      onClick={() => { setOpenId(p.id); setPanelTab(0); }}
                      onKeyDown={(e) => { if (e.key === 'Enter') { setOpenId(p.id); setPanelTab(0); } }}
                    >
                      <TableCell>
                        <Typography sx={{ fontWeight: 600 }}>
                          {p.name}
                          {/* A setup problem would stall their requests: flag it on the row. */}
                          {problemsById[p.id] && <Chip size="small" color="error" variant="outlined" label="Setup problem" sx={{ ml: 1 }} />}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                          {p.department || 'No department'} · {p.location || 'No location'}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        {p.managers === null ? '' : p.managers.length ? p.managers.join(', ') : <Chip size="small" color="error" variant="outlined" label="None" />}
                      </TableCell>
                      <Num v={p.balances?.vacation_balance} />
                      <Num v={p.balances?.sick_balance} />
                      <Num v={p.balances?.overtime} narrowHide />
                      <Num v={p.balances?.carryover} narrowHide />
                    </TableRow>
                  )),
                ])}
              </TableBody>
            </Table>
          </Box>
        )}
      </Paper>

      {/* The person panel: who they are, then their requests, managers and links. */}
      <Drawer anchor="right" open={!!person} onClose={() => setOpenId(null)} PaperProps={{ sx: { width: { xs: '100%', sm: 420 } } }}>
        {person && (
          <Box>
            <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'flex-start', p: 2, borderBottom: 1, borderColor: 'divider' }}>
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Typography sx={{ fontWeight: 700, fontSize: 17 }}>{person.name}</Typography>
                <Typography variant="body2" color="text.secondary">{person.email || 'No email'}</Typography>
                <Typography variant="body2" color="text.secondary">{person.department || 'No department'} · {person.location || 'No location'}</Typography>
                {problemsById[person.id] && (
                  <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap" sx={{ mt: 1 }}>
                    {problemsById[person.id].map((d) => <Chip key={d} size="small" color="error" variant="outlined" label={d} />)}
                  </Stack>
                )}
              </Box>
              <IconButton aria-label="Close" size="small" onClick={() => setOpenId(null)}><CloseIcon /></IconButton>
            </Box>
            <Tabs value={panelTab} onChange={(_, v) => setPanelTab(v)} sx={{ px: 1, borderBottom: 1, borderColor: 'divider' }}>
              <Tab label="Requests" />
              <Tab label="Managers" />
              <Tab label="Links" />
            </Tabs>

            <Box sx={{ p: 2 }}>
              {panelTab === 0 && (theirs.length === 0 ? (
                <Typography color="text.secondary">No requests yet.</Typography>
              ) : (
                <Stack divider={<Box sx={{ borderTop: 1, borderColor: 'divider' }} />}>
                  {theirs.map((r) => (
                    <Box key={`${r.request_type}-${r.id}`} sx={{ display: 'flex', gap: 1, alignItems: 'center', py: 1 }}>
                      <KindPill kind={requestKind(r)} label={requestWhat(r)} />
                      <Typography variant="body2" sx={{ flex: 1 }}>{requestWhen(r)}</Typography>
                      <Chip size="small" color={STATUS_COLOR[r.Status] || 'default'} label={r.Status || 'Pending'} />
                    </Box>
                  ))}
                </Stack>
              ))}

              {panelTab === 1 && (
                <Stack spacing={1.5}>
                  {person.managers === null ? (
                    <Typography color="text.secondary">Managers could not be loaded.</Typography>
                  ) : person.managers.length ? (
                    <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap">
                      {person.managers.map((m) => <Chip key={m} label={m} />)}
                    </Stack>
                  ) : (
                    <Typography variant="body2" color="error">No supervisor assigned. Their requests cannot reach anyone until one is set.</Typography>
                  )}
                  <Box><Button variant="outlined" onClick={onManageAssignments}>Change managers</Button></Box>
                </Stack>
              )}

              {panelTab === 2 && (
                <Stack spacing={2}>
                  <Box>
                    <Typography variant="body2" sx={{ fontWeight: 600, mb: 1 }}>Their own dashboard</Typography>
                    <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
                      <Button variant="contained" onClick={() => onOpenDashboard(person.id, 'employee')}>Open employee dashboard</Button>
                      <Button variant="outlined" onClick={() => onCopyLink(person.id)}>Copy link</Button>
                    </Stack>
                  </Box>
                  {/* Only someone who manages people has a team dashboard. */}
                  {person.is_manager && (
                    <Box>
                      <Typography variant="body2" sx={{ fontWeight: 600, mb: 1 }}>Their team dashboard</Typography>
                      <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
                        <Button variant="contained" onClick={() => onOpenDashboard(person.id, 'manager')}>Open team dashboard</Button>
                        <Button variant="outlined" onClick={() => onSendLink(person.id)}>Send dashboard link</Button>
                      </Stack>
                    </Box>
                  )}
                </Stack>
              )}
            </Box>
          </Box>
        )}
      </Drawer>
    </>
  );
}
