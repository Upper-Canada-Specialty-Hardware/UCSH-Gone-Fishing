import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Autocomplete, Box, Button, Chip, IconButton, MenuItem, Paper, TextField, Tooltip, Typography,
} from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import CloseIcon from '@mui/icons-material/Close';
import EventBusyIcon from '@mui/icons-material/EventBusy';
import KindPill from '../KindPill';
import PillToggle from '../PillToggle';
import { STATUS_COLOR } from '../dataGridDefaults';
import { Absence } from './teamStats';
import { requestKind, requestWhat, requestWhen } from '../../utils/requestText';
import { addDays, dayKey, formatDayRange, mondayOf, today } from '../../utils/dates';
import { isHourly } from '../../utils/employment';
import { kindColor } from '../../constants/leaveColors';
import {
  AddableMember, addTeamMember, getAddableMembers, getTeamMemberManagers,
  removeTeamMember, updateTeamEntitlements,
} from '../../api/client';

/** A tile filter key, or null for "show everyone". */
type FilterKey = 'waiting' | 'off' | 'low' | 'mu' | null;

/** A filter asked for by another tab (a Today tile), with a nonce so the same
 *  tile re-applies on a second click. */
export interface RequestedTeamFilter { key: Exclude<FilterKey, null>; nonce: number }

/**
 * One editable balance pot: its backend wire key, the balances field it reads,
 * its label and its - / + step. Managers change these with a reason (#362).
 */
interface Pot { key: string; balKey: string; label: string; step: number }

/** The seven pots, in the admin profile's order: two grants then five balances. */
const POTS: Pot[] = [
  { key: 'vacation', balKey: 'vacation_balance', label: 'Vacation left', step: 0.5 },
  { key: 'vacation_entitlement', balKey: 'vacation_entitlement', label: 'Vacation entitlement', step: 0.5 },
  { key: 'sick', balKey: 'sick_balance', label: 'Sick left', step: 0.5 },
  { key: 'sick_entitlement', balKey: 'sick_entitlement', label: 'Sick entitlement', step: 0.5 },
  { key: 'make_up', balKey: 'overtime', label: 'Make-Up', step: 0.125 },
  { key: 'carry_over', balKey: 'carryover', label: 'Carry-over', step: 0.5 },
  { key: 'payout', balKey: 'payout', label: 'Payout', step: 0.5 },
];
// Hourly staff have no vacation balance and no make-up/carry-over/payout pots
// (#300), so only these three may be changed for them.
const HOURLY_POT_KEYS = new Set(['vacation_entitlement', 'sick_entitlement', 'sick']);

/**
 * The pots a person's panel may change, by their type.
 *
 * @param hourly - Whether the person is hourly.
 * @returns The applicable pots.
 */
function potsFor(hourly: boolean): Pot[] {
  return hourly ? POTS.filter((p) => HOURLY_POT_KEYS.has(p.key)) : POTS;
}

/**
 * A short number: at most three decimals, trailing zeros trimmed.
 *
 * @param n - The value.
 * @returns The string.
 */
function fmt(n: number): string {
  return String(Math.round((Number(n) || 0) * 1000) / 1000);
}

/**
 * Initials for an avatar.
 *
 * @param name - The full name.
 * @returns Up to two letters.
 */
function initials(name: string): string {
  return (name || '').split(' ').map((w) => w[0] || '').slice(0, 2).join('');
}

interface Props {
  /** The team, each with a ``balances`` blob (from GET /team/members). */
  members: any[];
  /** The team's requests (from GET /team/requests), for recent-requests and sorting. */
  requests: any[];
  /** The waiting requests (from GET /team/pending), for the "Waiting on you" tile. */
  pending: any[];
  /** Approved + asked absences, for "off this week" and "coming up". */
  absences: Absence[];
  /** One stable colour per person, for the avatars. */
  colorOf: (name: string) => string;
  /** Whether writes are on; Save / Add / Remove are disabled when off. */
  processingEnabled: boolean;
  /** Whether the manager's location may log a sick day (#130); shows the button. */
  canLogSickDays: boolean;
  /** A filter asked for by a Today tile; applied when its nonce changes. */
  requestedFilter: RequestedTeamFilter | null;
  /** Called once the requested filter has been applied, so the parent can drop
   *  it and a later manual open of the tab does not re-apply a stale filter. */
  onFilterApplied: () => void;
  /** Open the sick-day dialog. */
  onLogSickDay: () => void;
  /** Open a person's request in the History tab. */
  onOpenHistory: (request: any) => void;
  /** Re-pull the team after a change (balances, add, remove) so the cards follow. */
  onRefresh: () => void;
  /** Surface a message on the dashboard snackbar. */
  onNotify: (message: string, severity: 'success' | 'error') => void;
}

/**
 * The manager dashboard's Team tab (#361, #362, #363): tiles that filter the
 * team, a Cards or Table view, and a person panel that shows balances, what is
 * coming up and recent requests and lets the manager change balances and
 * entitlements, add people, or remove themselves. Everything is drawn from the
 * team endpoints the dashboard already loads; the balance and membership
 * changes call the new ``/team`` manage endpoints.
 *
 * @param props - See {@link Props}.
 * @returns The Team tab body.
 */
export default function TeamTab({
  members, requests, pending, absences, colorOf, processingEnabled, canLogSickDays,
  requestedFilter, onFilterApplied, onLogSickDay, onOpenHistory, onRefresh, onNotify,
}: Props) {
  const theme = useTheme();
  const t = theme.tokens;
  const [filter, setFilter] = useState<FilterKey>(null);
  const [view, setView] = useState<'cards' | 'table'>('cards');
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<'name' | 'vac' | 'sick' | 'mu' | 'off'>('name');
  const [sortDir, setSortDir] = useState<1 | -1>(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);               // the Add-to-team picker is open

  // A Today tile asks for a filter by bumping a nonce; apply it once, then tell
  // the parent to drop it so a later manual open does not re-apply a stale one.
  const lastNonce = useRef<number | null>(null);
  useEffect(() => {
    if (requestedFilter && requestedFilter.nonce !== lastNonce.current) {
      lastNonce.current = requestedFilter.nonce;
      setFilter(requestedFilter.key);                        // jump straight to that filter
      onFilterApplied();                                     // parent clears it
    }
  }, [requestedFilter, onFilterApplied]);

  // The current working week (Monday to Friday), matching the Today "Off this
  // week" tile's count (teamStats.offThisWeek), so the tile and this filter agree.
  const weekStart = mondayOf(today());
  const weekEnd = addDays(weekStart, 4);

  /**
   * Whether a person has approved time off during this working week.
   *
   * @param name - The person's name.
   * @returns True when an approved absence overlaps Monday to Friday.
   */
  const offThisWeek = (name: string): boolean =>
    absences.some((a) => a.who === name && !a.pending && a.start <= weekEnd && a.end >= weekStart);

  /** The names with a request waiting on this manager, for the Waiting tile/filter. */
  const waitingNames = useMemo(
    () => new Set(pending.map((p) => p.employee_name).filter(Boolean)),
    [pending],
  );

  /**
   * A person's next time away from today on (approved or asked), earliest first.
   *
   * @param name - The person's name.
   * @returns The absence, or null when nothing is booked.
   */
  const nextOff = (name: string): Absence | null =>
    absences
      .filter((a) => a.who === name && a.end >= today())
      .sort((x, y) => x.start.getTime() - y.start.getTime())[0] || null;

  // The four tiles: label, count, note, and the people each one keeps.
  const tiles = useMemo(() => {
    const vacLeft = (m: any) => Number(m.balances?.vacation_balance ?? 0);
    const makeUp = (m: any) => Number(m.balances?.overtime ?? 0);
    const low = members.filter((m) => !isHourly(m.balances) && vacLeft(m) < 3);
    const off = members.filter((m) => offThisWeek(m.name));
    const waiting = members.filter((m) => waitingNames.has(m.name));
    const mu = members.filter((m) => makeUp(m) > 0);
    return [
      { key: 'waiting' as const, label: 'Waiting on you', count: waiting.length, note: 'with a request to decide', warn: true },
      { key: 'off' as const, label: 'Off this week', count: off.length, note: `of ${members.length} on the team` },
      { key: 'low' as const, label: 'Low on vacation', count: low.length, note: 'under 3 days left' },
      { key: 'mu' as const, label: 'Make-Up banked', count: mu.length, note: 'people with Make-Up' },
    ];
  }, [members, waitingNames, absences]);

  // The people the current tile and the search text keep.
  const shown = useMemo(() => {
    let list = members.slice();
    if (filter === 'low') list = list.filter((m) => !isHourly(m.balances) && Number(m.balances?.vacation_balance ?? 0) < 3);
    if (filter === 'off') list = list.filter((m) => offThisWeek(m.name));
    if (filter === 'waiting') list = list.filter((m) => waitingNames.has(m.name));
    if (filter === 'mu') list = list.filter((m) => Number(m.balances?.overtime ?? 0) > 0);
    const q = search.trim().toLowerCase();
    if (q) list = list.filter((m) => (m.name || '').toLowerCase().includes(q));
    // Sort by the chosen key; "off" sorts by the next off date, name as the fallback.
    const val = (m: any) => {
      if (sortKey === 'name') return (m.name || '').toLowerCase();
      if (sortKey === 'vac') return Number(m.balances?.vacation_balance ?? 0);
      if (sortKey === 'sick') return Number(m.balances?.sick_balance ?? 0);
      if (sortKey === 'mu') return Number(m.balances?.overtime ?? 0);
      const n = nextOff(m.name);                             // "off": soonest date first
      return n ? n.start.getTime() : Number.MAX_SAFE_INTEGER;
    };
    return list.sort((a, b) => {
      const av = val(a); const bv = val(b);
      return (av > bv ? 1 : av < bv ? -1 : 0) * sortDir;
    });
  }, [members, filter, search, sortKey, sortDir, waitingNames, absences]);

  // The open person, re-read from members so a change (or a removal) follows live.
  const selected = selectedId ? members.find((m) => String(m.id) === selectedId) || null : null;
  useEffect(() => {
    // The selected person left the team (removed, or filtered out by a refetch).
    if (selectedId && !members.some((m) => String(m.id) === selectedId)) setSelectedId(null);
  }, [members, selectedId]);

  /** Toggle a tile filter; clicking the active one clears it. @param key - The tile. */
  const toggleFilter = (key: Exclude<FilterKey, null>) => setFilter((f) => (f === key ? null : key));

  /** Sort by a column; clicking the active one flips direction. @param key - The column. */
  const sortBy = (key: typeof sortKey) => {
    if (sortKey === key) setSortDir((d) => (d === 1 ? -1 : 1));
    else { setSortKey(key); setSortDir(1); }
  };

  /** Add a picked person to this manager's team, then refresh. @param id - Staff id. */
  const doAdd = async (id: string, name: string) => {
    try {
      const res = await addTeamMember(id);
      onNotify(res.data.added ? `${name} added to your team.` : `${name} is already on your team.`, 'success');
      setAdding(false);
      onRefresh();                                           // pull the team again so the card appears
    } catch (err: any) {
      onNotify(err.response?.data?.detail || 'Could not add them to your team.', 'error');
    }
  };

  return (
    <>
      {/* Tiles: each filters the people below; the active one is outlined. */}
      <Box sx={{ display: 'grid', gap: 1.25, gridTemplateColumns: 'repeat(auto-fit, minmax(min(150px, 100%), 1fr))' }}>
        {tiles.map((tile) => {
          const on = filter === tile.key;
          return (
            <Box
              key={tile.key} component="button" type="button" aria-pressed={on}
              onClick={() => toggleFilter(tile.key)}
              sx={{
                textAlign: 'left', font: 'inherit', cursor: 'pointer', display: 'grid', gap: 0.25,
                p: 1.75, borderRadius: 2, border: 1,
                borderColor: on ? 'primary.main' : 'divider',
                boxShadow: on ? `inset 0 0 0 1px ${theme.palette.primary.main}` : 'none',
                bgcolor: on ? alpha(theme.palette.primary.main, 0.08) : 'background.paper',
                '&:hover': { borderColor: 'primary.main' },
              }}
            >
              <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>{tile.label}</Typography>
              <Typography sx={{ fontFamily: t.display, fontSize: 26, fontWeight: 600, lineHeight: 1.1, color: tile.warn && tile.count > 0 ? 'warning.main' : 'text.primary' }}>
                {tile.count}
              </Typography>
              <Typography variant="caption" color="text.secondary">{tile.note}</Typography>
            </Box>
          );
        })}
      </Box>

      {/* Toolbar: search, Cards/Table, Log sick day (where enabled), Add to my team. */}
      <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', flexWrap: 'wrap' }}>
        <TextField
          label="Search by name" value={search} onChange={(e) => setSearch(e.target.value)}
          size="small" placeholder="Start typing a name" sx={{ flex: '1 1 220px', maxWidth: 320 }}
        />
        <PillToggle
          name="team-view" label="Team view" value={view} onChange={setView}
          options={[{ key: 'cards', label: 'Cards' }, { key: 'table', label: 'Table' }]}
        />
        {canLogSickDays && (
          <Button variant="outlined" startIcon={<EventBusyIcon />} disabled={!processingEnabled} onClick={onLogSickDay}>
            Log sick day
          </Button>
        )}
        {adding ? (
          <AddPicker onPick={doAdd} onClose={() => setAdding(false)} />
        ) : (
          <Tooltip title={processingEnabled ? '' : 'Adding is off while the system is in reporting-only mode.'}>
            <span>
              <Button variant="contained" disabled={!processingEnabled} onClick={() => setAdding(true)}>
                Add to my team
              </Button>
            </span>
          </Tooltip>
        )}
      </Box>

      {filter && (
        <Typography variant="caption" color="text.secondary">
          Filtered by a tile. Click it again to show everyone.
        </Typography>
      )}

      {/* The list (cards or table) beside the person panel when one is open. */}
      <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: selected ? 'minmax(0, 1.4fr) minmax(0, 1fr)' : 'minmax(0, 1fr)' }, alignItems: 'start' }}>
        <Box sx={{ minWidth: 0 }}>
          {shown.length === 0 ? (
            <Paper sx={{ p: 4, textAlign: 'center', color: 'text.secondary' }}>Nobody matches this tile.</Paper>
          ) : view === 'cards' ? (
            <Box sx={{ display: 'grid', gap: 1.5, gridTemplateColumns: 'repeat(auto-fill, minmax(min(220px, 100%), 1fr))' }}>
              {shown.map((m) => (
                <MemberCard
                  key={m.id} member={m} colorOf={colorOf} current={String(m.id) === selectedId} nextOff={nextOff(m.name)}
                  onOpen={() => setSelectedId((id) => (id === String(m.id) ? null : String(m.id)))}
                />
              ))}
            </Box>
          ) : (
            <MemberTable
              members={shown} sortKey={sortKey} sortDir={sortDir} onSort={sortBy}
              selectedId={selectedId} nextOffOf={nextOff}
              onOpen={(id) => setSelectedId((cur) => (cur === id ? null : id))}
            />
          )}
        </Box>
        {selected && (
          <PersonPanel
            member={selected} requests={requests} absences={absences} processingEnabled={processingEnabled}
            onClose={() => setSelectedId(null)} onOpenHistory={onOpenHistory}
            onRefresh={onRefresh} onNotify={onNotify}
          />
        )}
      </Box>
    </>
  );
}

/**
 * The inline "Add to my team" picker: it loads addable staff and adds the one
 * the manager chooses.
 *
 * @param props.onPick - Called with the chosen person's id and name.
 * @param props.onClose - Close the picker without adding.
 * @returns The autocomplete.
 */
function AddPicker({ onPick, onClose }: { onPick: (id: string, name: string) => void; onClose: () => void }) {
  const [options, setOptions] = useState<AddableMember[]>([]);
  useEffect(() => {
    let live = true;
    getAddableMembers().then((r) => { if (live) setOptions(r.data.people || []); }).catch(() => { if (live) setOptions([]); });
    return () => { live = false; };
  }, []);
  return (
    <Autocomplete
      openOnFocus size="small" sx={{ minWidth: 260 }} options={options}
      getOptionLabel={(o) => o.name}
      isOptionEqualToValue={(a, b) => a.id === b.id}
      onChange={(_, picked) => { if (picked) onPick(picked.id, picked.name); }}
      onBlur={onClose}
      renderOption={(props, o) => (
        <li {...props} key={o.id}>
          <Box>
            <Typography sx={{ fontSize: 14 }}>{o.name}</Typography>
            <Typography variant="caption" color="text.secondary">{o.department || 'No department'} · {o.location || 'No location'}</Typography>
          </Box>
        </li>
      )}
      renderInput={(params) => <TextField {...params} autoFocus placeholder="Search staff to add" aria-label="Add to my team" />}
    />
  );
}

/**
 * One team member card: avatar, name, placement, a vacation-left bar (hourly
 * show a vacation-taken counter), sick left, and their next time off.
 *
 * @param props.member - The member and their balances.
 * @param props.colorOf - The avatar colour by name.
 * @param props.current - Whether this card's person is open in the panel.
 * @param props.nextOff - Their next absence, or null.
 * @param props.onOpen - Open this person in the panel.
 * @returns The card.
 */
function MemberCard({ member, colorOf, current, nextOff, onOpen }: {
  member: any; colorOf: (n: string) => string; current: boolean; nextOff: Absence | null; onOpen: () => void;
}) {
  const theme = useTheme();
  const b = member.balances || {};
  const hourly = isHourly(b);
  const vac = Number(b.vacation_balance ?? 0);
  const of = Number(b.vacation_entitlement ?? 0);
  const low = vac < 3;
  const pct = of > 0 ? Math.max(4, Math.min(100, (vac / of) * 100)) : 0;
  return (
    <Box
      component="button" type="button" aria-current={current} onClick={onOpen}
      sx={{
        textAlign: 'left', font: 'inherit', cursor: 'pointer', display: 'grid', gap: 1,
        p: 1.75, borderRadius: 2, border: 1, bgcolor: 'background.paper',
        borderColor: current ? 'primary.main' : 'divider',
        boxShadow: current ? `inset 0 0 0 1px ${theme.palette.primary.main}` : 'none',
        '&:hover': { borderColor: theme.tokens.line2, bgcolor: theme.tokens.hover },
      }}
    >
      <Box sx={{ display: 'flex', gap: 1.25, alignItems: 'center' }}>
        <Box sx={{ width: 34, height: 34, borderRadius: 999, flex: 'none', bgcolor: colorOf(member.name), color: theme.palette.mode === 'dark' ? theme.palette.background.default : '#fff', display: 'grid', placeItems: 'center', fontWeight: 700, fontSize: 12.5 }}>
          {initials(member.name)}
        </Box>
        <Box sx={{ minWidth: 0 }}>
          <Typography sx={{ fontWeight: 700, overflowWrap: 'anywhere' }}>{member.name}</Typography>
          <Typography variant="caption" color="text.secondary">{member.department || 'No department'} · {member.employment_type || 'Salaried'}</Typography>
        </Box>
      </Box>
      {hourly ? (
        <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
          <Typography variant="body2" color="text.secondary">Vacation taken</Typography>
          <Typography variant="body2" sx={{ fontWeight: 700 }}>{fmt(b.vacation_taken ?? 0)}</Typography>
        </Box>
      ) : (
        <Box sx={{ display: 'grid', gap: 0.5 }}>
          <Box sx={{ display: 'flex', justifyContent: 'space-between', fontSize: 12.5, color: 'text.secondary' }}>
            <span>Vacation left</span><b style={{ color: low ? theme.palette.error.main : undefined }}>{fmt(vac)} of {fmt(of)}</b>
          </Box>
          {/* The bar fills to vacation-left; red when under three days. */}
          <Box sx={{ height: 7, borderRadius: 999, bgcolor: theme.tokens.chip, overflow: 'hidden' }}>
            <Box sx={{ height: '100%', width: `${pct}%`, borderRadius: 999, bgcolor: low ? theme.palette.error.main : kindColor('vacation', theme.palette.mode) }} />
          </Box>
        </Box>
      )}
      <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
        <Typography variant="body2" color="text.secondary">Sick left</Typography>
        <Typography variant="body2" sx={{ fontWeight: 700 }}>{fmt(b.sick_balance ?? 0)}</Typography>
      </Box>
      <Typography variant="caption" sx={{ color: kindColor('vacation', theme.palette.mode), fontWeight: 600 }}>
        {nextOff ? `Next off: ${formatDayRange(dayKey(nextOff.start), dayKey(nextOff.end))}${nextOff.pending ? ' (asked)' : ''}` : 'No time off booked'}
      </Typography>
    </Box>
  );
}

/**
 * The team as a sortable table that never scrolls sideways: it drops the Make-Up
 * and next-off columns on a phone and wraps names instead of scrolling.
 *
 * @param props.members - The people to list (already filtered and sorted).
 * @param props.sortKey - The active sort column.
 * @param props.sortDir - 1 ascending, -1 descending.
 * @param props.onSort - Sort by a column.
 * @param props.selectedId - The open person's id.
 * @param props.nextOffOf - A person's next absence.
 * @param props.onOpen - Open a person in the panel.
 * @returns The table.
 */
function MemberTable({ members, sortKey, sortDir, onSort, selectedId, nextOffOf, onOpen }: {
  members: any[]; sortKey: string; sortDir: 1 | -1; onSort: (k: any) => void;
  selectedId: string | null; nextOffOf: (n: string) => Absence | null; onOpen: (id: string) => void;
}) {
  const theme = useTheme();
  // Person always; vacation and sick always; Make-Up and Next off only from sm up.
  const cols = { xs: 'minmax(0, 1.4fr) auto auto', sm: 'minmax(0, 1.6fr) auto auto auto minmax(0, 1.3fr)' };
  const arrow = (k: string) => (sortKey === k ? (sortDir === 1 ? ' ↑' : ' ↓') : '');
  const head = (k: string, label: string, hideXs = false, num = false) => (
    <Box
      component="button" type="button" onClick={() => onSort(k)}
      sx={{ display: hideXs ? { xs: 'none', sm: 'inline' } : 'inline', textAlign: num ? 'right' : 'left', border: 0, background: 'none', p: 0, font: 'inherit', fontWeight: 700, fontSize: 12.5, color: 'text.secondary', cursor: 'pointer' }}
    >
      {label}{arrow(k)}
    </Box>
  );
  return (
    <Box sx={{ borderTop: 1, borderColor: 'divider' }}>
      <Box sx={{ display: 'grid', gridTemplateColumns: cols, gap: 1.5, alignItems: 'center', px: 1, py: 1, bgcolor: theme.tokens.hover, borderRadius: 1, mt: 1 }}>
        {head('name', 'Person')}
        {head('vac', 'Vac left', false, true)}
        {head('sick', 'Sick', false, true)}
        {head('mu', 'Make-Up', true, true)}
        {head('off', 'Next off', true)}
      </Box>
      {members.map((m) => {
        const b = m.balances || {};
        const vac = Number(b.vacation_balance ?? 0);
        const n = nextOffOf(m.name);
        const open = String(m.id) === selectedId;
        return (
          <Box
            key={m.id} component="button" type="button" aria-current={open} onClick={() => onOpen(String(m.id))}
            sx={{
              width: '100%', textAlign: 'left', font: 'inherit', cursor: 'pointer',
              display: 'grid', gridTemplateColumns: cols, gap: 1.5, alignItems: 'center', px: 1, py: 1.25,
              border: 0, borderBottom: 1, borderColor: 'divider', background: open ? theme.tokens.hover : 'none',
              '&:hover': { background: theme.tokens.hover },
            }}
          >
            <Box sx={{ minWidth: 0 }}>
              <Typography sx={{ fontWeight: 700, fontSize: 14, overflowWrap: 'anywhere' }}>{m.name}</Typography>
              <Typography variant="caption" color="text.secondary">{m.department || 'No department'}</Typography>
            </Box>
            <Typography sx={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontWeight: 700, color: !isHourly(b) && vac < 3 ? 'error.main' : 'text.primary' }}>
              {isHourly(b) ? '-' : fmt(vac)}
            </Typography>
            <Typography sx={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{fmt(b.sick_balance ?? 0)}</Typography>
            <Typography sx={{ display: { xs: 'none', sm: 'block' }, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{fmt(b.overtime ?? 0)}</Typography>
            <Typography variant="body2" sx={{ display: { xs: 'none', sm: 'block' }, color: 'text.secondary' }}>
              {n ? `${formatDayRange(dayKey(n.start), dayKey(n.end))}${n.pending ? ' (asked)' : ''}` : 'None booked'}
            </Typography>
          </Box>
        );
      })}
    </Box>
  );
}

/**
 * The person panel: balances, what is coming up, recent requests, and a Manage
 * block to change a balance or entitlement (#362) or remove the manager (#363).
 *
 * @param props.member - The open person and their balances.
 * @param props.requests - The team's requests, filtered here to this person.
 * @param props.absences - Absences, for the coming-up line.
 * @param props.processingEnabled - Whether writes are on.
 * @param props.onClose - Close the panel.
 * @param props.onOpenHistory - Open one of their requests in the History tab.
 * @param props.onRefresh - Re-pull the team after a change.
 * @param props.onNotify - Surface a message.
 * @returns The panel.
 */
function PersonPanel({ member, requests, absences, processingEnabled, onClose, onOpenHistory, onRefresh, onNotify }: {
  member: any; requests: any[]; absences: Absence[]; processingEnabled: boolean;
  onClose: () => void; onOpenHistory: (r: any) => void; onRefresh: () => void;
  onNotify: (m: string, s: 'success' | 'error') => void;
}) {
  const theme = useTheme();
  const t = theme.tokens;
  const b = member.balances || {};
  const hourly = isHourly(b);
  const [mode, setMode] = useState<null | 'adjust' | 'remove'>(null);
  // The remove confirmation needs to know whether another manager would remain.
  const [otherManager, setOtherManager] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  // The person's own requests, newest first, for the recent list.
  const theirRequests = useMemo(
    () => requests
      .filter((r) => (r.employee_name || '') === member.name)
      .sort((a, c) => String(c.StartDate || '').localeCompare(String(a.StartDate || '')))
      .slice(0, 6),
    [requests, member.name],
  );
  // Their next time away, for the coming-up line.
  const soon = absences
    .filter((a) => a.who === member.name && a.end >= today())
    .sort((x, y) => x.start.getTime() - y.start.getTime())[0];

  // Reset the manage state whenever a different person opens.
  useEffect(() => { setMode(null); setOtherManager(null); }, [member.id]);

  /** Open the remove confirmation, first reading whether anyone else manages them. */
  const openRemove = async () => {
    setMode('remove');
    setOtherManager(null);
    try {
      const res = await getTeamMemberManagers(String(member.id));
      setOtherManager(Boolean(res.data.has_other_manager));
    } catch {
      setOtherManager(null);                                 // unknown: the warning stays cautious
    }
  };

  /** Remove this manager from the person, then refresh and close. */
  const doRemove = async () => {
    setBusy(true);
    try {
      await removeTeamMember(String(member.id));
      onNotify(`${member.name} removed from your team.`, 'success');
      onClose();
      onRefresh();
    } catch (err: any) {
      onNotify(err.response?.data?.detail || 'Could not remove them from your team.', 'error');
    } finally {
      setBusy(false);
    }
  };

  const pots = potsFor(hourly);

  return (
    <Paper sx={{ p: 2, display: 'grid', gap: 2, alignContent: 'start', minWidth: 0, border: 1, borderColor: t.line2 }}>
      {/* Head: name, placement, close. */}
      <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 1, alignItems: 'start' }}>
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="h6" sx={{ overflowWrap: 'anywhere' }}>{member.name}</Typography>
          <Typography variant="caption" color="text.secondary">{member.department || 'No department'} · {member.employment_type || 'Salaried'}</Typography>
        </Box>
        <IconButton aria-label="Close" size="small" onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
      </Box>

      {/* Pots: hourly show a vacation-taken counter plus sick; salaried show all four. */}
      <Box sx={{ display: 'grid', gap: 1, gridTemplateColumns: 'repeat(auto-fit, minmax(min(110px, 100%), 1fr))' }}>
        {hourly ? (
          <>
            <PotTile label="vacation taken" value={b.vacation_taken ?? 0} />
            <PotTile label="sick left" value={b.sick_balance ?? 0} />
          </>
        ) : (
          <>
            <PotTile label={`vacation left of ${fmt(b.vacation_entitlement ?? 0)}`} value={b.vacation_balance ?? 0} />
            <PotTile label="sick left" value={b.sick_balance ?? 0} />
            <PotTile label="Make-Up" value={b.overtime ?? 0} />
            <PotTile label="carry-over" value={b.carryover ?? 0} />
          </>
        )}
      </Box>

      {/* Coming up. */}
      <Box>
        <PanelHead>Coming up</PanelHead>
        <Typography variant="body2" color="text.secondary">
          {soon ? `${formatDayRange(dayKey(soon.start), dayKey(soon.end))}${soon.pending ? ' (asked)' : ''}` : 'Nothing booked.'}
        </Typography>
      </Box>

      {/* Recent requests: each opens its full row in History. */}
      <Box>
        <PanelHead>Recent requests</PanelHead>
        {theirRequests.length === 0 ? (
          <Typography variant="body2" color="text.secondary">No requests this year.</Typography>
        ) : (
          <Box sx={{ display: 'grid' }}>
            {theirRequests.map((r) => {
              const status = r.Status || 'Pending';
              return (
                <Box
                  key={`${r.request_type}-${r.id}`} component="button" type="button" onClick={() => onOpenHistory(r)}
                  sx={{
                    display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap', width: '100%', textAlign: 'left',
                    border: 0, borderBottom: 1, borderColor: 'divider', background: 'none', font: 'inherit', cursor: 'pointer',
                    py: 0.75, '&:hover': { background: t.hover },
                  }}
                >
                  <KindPill kind={requestKind(r)} label={requestWhat(r)} />
                  <Typography variant="body2" color="text.secondary" sx={{ flex: '1 1 auto', minWidth: 0 }}>{requestWhen(r)}</Typography>
                  <Chip size="small" color={STATUS_COLOR[status] || 'default'} label={status} />
                </Box>
              );
            })}
            <Typography variant="caption" color="text.secondary" sx={{ mt: 0.75 }}>Each request opens its full row in History.</Typography>
          </Box>
        )}
      </Box>

      {/* Manage: change a balance/entitlement, or remove from the team. */}
      <Box sx={{ display: 'grid', gap: 1 }}>
        <PanelHead>Manage</PanelHead>
        {mode === 'adjust' ? (
          <AdjustForm
            member={member} pots={pots} processingEnabled={processingEnabled}
            onDone={() => { setMode(null); onRefresh(); }} onCancel={() => setMode(null)} onNotify={onNotify}
          />
        ) : mode === 'remove' ? (
          <Box sx={{ display: 'grid', gap: 1, p: 1.5, borderRadius: 2, border: 1, borderColor: 'error.main', bgcolor: alpha(theme.palette.error.main, 0.08) }}>
            <Typography sx={{ fontWeight: 700 }}>Remove {member.name} from your team?</Typography>
            <Typography variant="body2" color="text.secondary">
              {otherManager === null
                ? 'Checking whether anyone else manages them...'
                : otherManager
                  ? 'Another manager stays on them, so their requests still have somewhere to go.'
                  : 'You are their only manager. Their requests will have nobody to go to until an admin or another manager adds them.'}
            </Typography>
            <Box sx={{ display: 'flex', gap: 1, justifyContent: 'flex-end' }}>
              <Button size="small" onClick={() => setMode(null)} disabled={busy}>Keep</Button>
              <Button size="small" color="error" variant="contained" onClick={doRemove} disabled={busy || !processingEnabled}>
                {busy ? 'Removing...' : 'Remove'}
              </Button>
            </Box>
          </Box>
        ) : (
          <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
            <Tooltip title={processingEnabled ? '' : 'Changes are off while the system is in reporting-only mode.'}>
              <span>
                <Button size="small" variant="outlined" disabled={!processingEnabled} onClick={() => setMode('adjust')}>
                  Change a balance or entitlement
                </Button>
              </span>
            </Tooltip>
            <Button size="small" variant="outlined" color="error" onClick={openRemove}>Remove from my team</Button>
          </Box>
        )}
      </Box>
    </Paper>
  );
}

/** A small uppercase section heading inside the panel. */
function PanelHead({ children }: { children: React.ReactNode }) {
  return (
    <Typography sx={{ fontSize: 12, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: 'text.secondary', mb: 0.5 }}>
      {children}
    </Typography>
  );
}

/**
 * One read-only balance tile in the panel.
 *
 * @param props.label - What it measures.
 * @param props.value - The days.
 * @returns The tile.
 */
function PotTile({ label, value }: { label: string; value: number }) {
  const theme = useTheme();
  return (
    <Box sx={{ bgcolor: theme.tokens.hover, borderRadius: 2, p: 1.25 }}>
      <Typography sx={{ fontFamily: theme.tokens.display, fontSize: 20, fontWeight: 600, fontVariantNumeric: 'tabular-nums', color: Number(value) < 0 ? 'error.main' : 'text.primary' }}>{fmt(value)}</Typography>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
    </Box>
  );
}

/**
 * The "change a balance or entitlement" form: pick a pot, enter a new value and
 * a required reason, and save through the team entitlements endpoint (#362).
 *
 * @param props.member - The person being changed.
 * @param props.pots - The pots that may be changed (fewer for hourly staff).
 * @param props.processingEnabled - Whether saving is on.
 * @param props.onDone - Called after a successful save.
 * @param props.onCancel - Drop the form.
 * @param props.onNotify - Surface a message.
 * @returns The form.
 */
function AdjustForm({ member, pots, processingEnabled, onDone, onCancel, onNotify }: {
  member: any; pots: Pot[]; processingEnabled: boolean;
  onDone: () => void; onCancel: () => void; onNotify: (m: string, s: 'success' | 'error') => void;
}) {
  const b = member.balances || {};
  const [potKey, setPotKey] = useState(pots[0].key);
  const pot = pots.find((p) => p.key === potKey) || pots[0];
  const current = Number(b[pot.balKey] ?? 0);
  const [value, setValue] = useState(String(current));
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  // Switching pots prefills the new pot's current value.
  useEffect(() => { setValue(String(Number(b[pot.balKey] ?? 0))); }, [potKey]);

  /** Validate and save the one change with its reason. */
  const save = async () => {
    const num = parseFloat(value);
    if (Number.isNaN(num)) { setError('Enter a number.'); return; }
    if (!reason.trim()) { setError('A reason is required.'); return; }
    setError('');
    setSaving(true);
    try {
      const res = await updateTeamEntitlements(String(member.id), { changes: { [pot.key]: num }, reason: reason.trim() });
      onNotify(res.data.notice || `${pot.label} updated for ${member.name}.`, res.data.notice ? 'error' : 'success');
      onDone();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'The change could not be saved.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Box sx={{ display: 'grid', gap: 1 }}>
      <TextField select size="small" label="Balance" value={potKey} onChange={(e) => setPotKey(e.target.value)}>
        {pots.map((p) => <MenuItem key={p.key} value={p.key}>{p.label}</MenuItem>)}
      </TextField>
      <TextField
        size="small" type="number" label="New value (days)" value={value}
        onChange={(e) => setValue(e.target.value)} inputProps={{ step: pot.step }}
      />
      <TextField
        size="small" label="Reason" value={reason} onChange={(e) => setReason(e.target.value)}
        placeholder="For example: corrected after payroll check"
      />
      {error && <Typography variant="caption" color="error">{error}</Typography>}
      <Box sx={{ display: 'flex', gap: 1, justifyContent: 'flex-end' }}>
        <Button size="small" onClick={onCancel} disabled={saving}>Cancel</Button>
        <Button size="small" variant="contained" onClick={save} disabled={saving || !processingEnabled}>
          {saving ? 'Saving...' : 'Save'}
        </Button>
      </Box>
    </Box>
  );
}
