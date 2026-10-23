import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Autocomplete, Box, Button, ClickAwayListener, IconButton, Paper, TextField, Tooltip, Typography,
} from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import CloseIcon from '@mui/icons-material/Close';
import EventBusyIcon from '@mui/icons-material/EventBusy';
import { AnimatePresence, m } from 'motion/react';
import KindPill from '../KindPill';
import PillToggle from '../PillToggle';
import { Panel } from '../Panels';
import { EXIT, riseAt } from '../Motion';
import PersonProfile from '../admin/PersonProfile';
import { VacationRings } from './Charts';
import { Absence } from './teamStats';
import { addDays, dayKey, formatDayRange, mondayOf, today } from '../../utils/dates';
import { isHourly } from '../../utils/employment';
import { kindColor } from '../../constants/leaveColors';
import { StaffRecord } from '../../api/staff';
import {
  AddableMember, addTeamMember, getAddableMembers, getTeamMemberRecord,
} from '../../api/client';

/** A tile filter key, or null for "show everyone". */
type FilterKey = 'waiting' | 'off' | 'low' | 'mu' | null;

/** A filter asked for by another tab (a Today tile), with a nonce so the same
 *  tile re-applies on a second click. */
export interface RequestedTeamFilter { key: Exclude<FilterKey, null>; nonce: number }

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
  /** The team's requests (from GET /team/requests), for the person profile's activity. */
  requests: any[];
  /** The waiting requests (from GET /team/pending), for the "Waiting on you" tile. */
  pending: any[];
  /** Approved + asked absences, for "off this week" and "coming up". */
  absences: Absence[];
  /** One stable colour per person, for the avatars and the vacation rings. */
  colorOf: (name: string) => string;
  /** One short, unambiguous label per person, for the vacation rings. */
  labelOf: (name: string) => string;
  /** The signed-in manager's own name, for the person profile's manager mode (#372). */
  managerName: string;
  /** Whether writes are on; Save / Add / Remove are disabled when off. */
  processingEnabled: boolean;
  /** Whether the manager's location may log a sick day (#130); shows the button. */
  canLogSickDays: boolean;
  /** A filter asked for by a Today tile; applied when its nonce changes. */
  requestedFilter: RequestedTeamFilter | null;
  /** Called once the requested filter has been applied, so the parent can drop
   *  it and a later manual open of the tab does not re-apply a stale filter. */
  onFilterApplied: () => void;
  /** A person the Calendar tab asked to open (#373), with a nonce so the same
   *  person re-opens on a second ask; applied once, then cleared. */
  requestedPerson: { id: string; nonce: number } | null;
  /** Called once the requested person has been opened, so the parent drops it. */
  onPersonRequested: () => void;
  /** Open the Add-a-new-hire screen (the dashboard's "add" tab). */
  onAddNewHire: () => void;
  /** Open the sick-day dialog. */
  onLogSickDay: () => void;
  /** Re-pull the team after a change (balances, add, remove) so the cards follow. */
  onRefresh: () => void;
  /** Surface a message on the dashboard snackbar. */
  onNotify: (message: string, severity: 'success' | 'error') => void;
}

/**
 * The manager dashboard's Team tab (#361, #362, #363, #372): tiles that filter
 * the team, a Cards or Table view, the vacation rings and a card per person in
 * the old style, plus a dashed "add a new hire" card and an inline "add to my
 * team" module. Clicking a person opens the admin person profile in manager mode
 * in the grid's place, so a manager gets the same balances, entitlements,
 * details and activity view admins get, scoped to their own team.
 *
 * @param props - See {@link Props}.
 * @returns The Team tab body.
 */
export default function TeamTab({
  members, requests, pending, absences, colorOf, labelOf, managerName, processingEnabled,
  canLogSickDays, requestedFilter, onFilterApplied, requestedPerson, onPersonRequested,
  onAddNewHire, onLogSickDay, onRefresh, onNotify,
}: Props) {
  const theme = useTheme();
  const t = theme.tokens;
  const [filter, setFilter] = useState<FilterKey>(null);
  const [view, setView] = useState<'cards' | 'table'>('cards');
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<'name' | 'vac' | 'sick' | 'mu' | 'off'>('name');
  const [sortDir, setSortDir] = useState<1 | -1>(1);
  const [adding, setAdding] = useState(false);               // the inline "add to my team" module is open
  const [selectedId, setSelectedId] = useState<string | null>(null);  // the open person's id
  const [record, setRecord] = useState<StaffRecord | null>(null);     // their fetched full record
  const [recordLoading, setRecordLoading] = useState(false);

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

  // The people the current tile and the search text keep, sorted by the column.
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

  // Cards split into Salaried and Hourly bands (#300, #338): salaried keep every
  // pot, hourly show a vacation-taken counter and Sick only.
  const salariedShown = useMemo(() => shown.filter((m) => !isHourly(m.balances)), [shown]);
  const hourlyShown = useMemo(() => shown.filter((m) => isHourly(m.balances)), [shown]);

  // The open person's summary, re-read from members so a removal elsewhere closes
  // the profile; their requests feed the profile's Activity list.
  const selected = selectedId ? members.find((m) => String(m.id) === selectedId) || null : null;
  const theirRequests = useMemo(
    () => (selected ? requests.filter((r) => (r.employee_name || '') === selected.name) : []),
    [requests, selected],
  );
  useEffect(() => {
    // The open person left the team (removed, or filtered out by a refetch).
    if (selectedId && !members.some((m) => String(m.id) === selectedId)) {
      setSelectedId(null);
      setRecord(null);
    }
  }, [members, selectedId]);

  /** Toggle a tile filter; clicking the active one clears it. @param key - The tile. */
  const toggleFilter = (key: Exclude<FilterKey, null>) => setFilter((f) => (f === key ? null : key));

  /** Sort by a column; clicking the active one flips direction. @param key - The column. */
  const sortBy = (key: typeof sortKey) => {
    if (sortKey === key) setSortDir((d) => (d === 1 ? -1 : 1));
    else { setSortKey(key); setSortDir(1); }
  };

  /**
   * Open a person: fetch their full record, then show the profile in the grid's
   * place. Mirrors the admin staff screen's openPerson.
   *
   * @param id - Staff Directory id.
   */
  const openPerson = async (id: string) => {
    setSelectedId(id);
    setRecord(null);
    setRecordLoading(true);
    try {
      const res = await getTeamMemberRecord(id);             // GET /team/members/{id}/record
      setRecord(res.data);
    } catch {
      onNotify('That person could not be loaded.', 'error');
      setSelectedId(null);
    } finally {
      setRecordLoading(false);
    }
  };

  /** Re-read the team and the open person's record after a save, so both follow. */
  const reloadRecord = async () => {
    onRefresh();                                             // the team cards and the waiting count
    if (!selectedId) return;
    try {
      const res = await getTeamMemberRecord(selectedId);     // the profile's own record
      setRecord(res.data);
    } catch {
      /* the save already landed; the view just keeps the pre-save figures */
    }
  };

  /** Add a picked person to this manager's team, then refresh. @param id - Staff id. @param name - Their name. */
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

  // The Calendar tab asked to open a person (#373): open them once per nonce,
  // then tell the parent to drop the request so it is not re-applied on return.
  const lastPersonNonce = useRef<number | null>(null);
  useEffect(() => {
    if (requestedPerson && requestedPerson.nonce !== lastPersonNonce.current) {
      lastPersonNonce.current = requestedPerson.nonce;
      openPerson(requestedPerson.id);                      // fetch the record and show the profile
      onPersonRequested();                                 // parent clears it
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedPerson]);

  // A person is open: the admin profile in manager mode takes the grid's place,
  // exactly as the admin staff screen replaces its table (#372).
  if (selectedId && record) {
    return (
      <PersonProfile
        mode="manager"
        managerName={managerName}
        record={record}
        requests={theirRequests}
        users={[]}
        processingEnabled={processingEnabled}
        onBack={() => { setSelectedId(null); setRecord(null); }}
        onSaved={reloadRecord}
        onRemovedSelf={() => { setSelectedId(null); setRecord(null); onRefresh(); }}
        onNotify={onNotify}
      />
    );
  }
  if (selectedId && recordLoading) {
    return <Paper sx={{ p: 4 }}><Typography color="text.secondary">Loading the person...</Typography></Paper>;
  }

  /**
   * One team member's balance card, in the pre-#361 style: avatar, placement,
   * the balance lines for their type, and their next time off. The card lifts
   * under the pointer and opens the person's profile on click.
   *
   * @param mem - The member and their balances.
   * @param k - The card's index in its group, for the entrance stagger.
   * @returns The animated, clickable card.
   */
  const renderMemberCard = (mem: any, k: number) => {
    const b = mem.balances || {};
    const n = nextOff(mem.name);                             // their next time away, approved or asked
    const hourly = isHourly(b);                              // hourly: Sick plus a vacation counter
    /** One balance line. @param label - Pot. @param v - Days. @param of - Entitlement. @returns The line. */
    const line = (label: string, v: number, of?: number) => (
      <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
        <Typography variant="body2" color="text.secondary">{label}</Typography>
        <Typography variant="body2" sx={{ fontWeight: 700, color: v < 0 ? 'error.main' : 'text.primary' }}>{fmt(v)}{of ? ` of ${fmt(of)}` : ''}</Typography>
      </Box>
    );
    return (
      <m.div
        key={mem.id}
        layout
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.96, transition: EXIT }}
        style={{ display: 'grid' }}
      >
        {/* A card lifts slightly under the pointer so it reads as one object. */}
        <Paper
          role="button" tabIndex={0} aria-label={`Open ${mem.name}`}
          onClick={() => openPerson(String(mem.id))}
          onKeyDown={(e) => { if (e.key === 'Enter') openPerson(String(mem.id)); }}
          sx={{
            p: 2, display: 'grid', gap: 0.75, alignContent: 'start', cursor: 'pointer', ...riseAt(k, { step: 40, max: 8 }),
            transition: 'transform .2s cubic-bezier(.2,.8,.2,1), border-color .2s ease',
            '&:hover': { transform: 'translateY(-2px)', borderColor: t.line2 },
          }}
        >
          <Box sx={{ display: 'flex', gap: 1.25, alignItems: 'center', mb: 0.5 }}>
            <Box sx={{ width: 36, height: 36, borderRadius: 999, bgcolor: colorOf(mem.name), color: theme.palette.mode === 'dark' ? theme.palette.background.default : '#fff', display: 'grid', placeItems: 'center', fontWeight: 700, fontSize: 13, flex: 'none' }}>
              {initials(mem.name)}
            </Box>
            <Box sx={{ minWidth: 0 }}>
              {/* Wrap a long name onto a second line instead of cutting it off. */}
              <Typography sx={{ fontWeight: 700, overflowWrap: 'anywhere', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{mem.name}</Typography>
              <Typography variant="caption" color="text.secondary">{mem.department || 'No department'} · {mem.location || 'No location'}</Typography>
            </Box>
          </Box>
          {hourly ? (
            <>
              {/* Vacation is a counter here, not a balance: no "of", with a caption. */}
              <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                <Typography variant="body2" color="text.secondary">Vacation taken</Typography>
                <Typography variant="body2" sx={{ fontWeight: 700 }}>{fmt(b.vacation_taken ?? 0)}</Typography>
              </Box>
              <Typography variant="caption" color="text.secondary" sx={{ mt: -0.5 }}>This year. Counted, not a balance.</Typography>
              {line('Sick', b.sick_balance ?? 0, b.sick_entitlement)}
            </>
          ) : (
            <>
              {line('Vacation', b.vacation_balance ?? 0, b.vacation_entitlement)}
              {line('Sick', b.sick_balance ?? 0, b.sick_entitlement)}
              {line('Make-Up', b.overtime ?? 0)}
              {line('Carry Over', b.carryover ?? 0)}
            </>
          )}
          <Box sx={{ mt: 0.5, pt: 1, borderTop: 1, borderColor: 'divider' }}>
            {n ? (
              <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                <Typography variant="body2" color="text.secondary">Next off</Typography>
                {n.kind && <KindPill kind={n.kind} label={n.label} />}
                <Typography variant="body2">
                  {/* Full day format; formatDayRange shows one date when start == end. */}
                  {formatDayRange(dayKey(n.start), dayKey(n.end))}{n.pending ? ' (asked)' : ''}
                </Typography>
              </Box>
            ) : <Typography variant="body2" color="text.secondary">No time off booked.</Typography>}
          </Box>
        </Paper>
      </m.div>
    );
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

      {/* Toolbar: search, Cards/Table, and Log sick day (where enabled). */}
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
          <Button variant="outlined" startIcon={<EventBusyIcon />} disabled={!processingEnabled} onClick={onLogSickDay} sx={{ ml: { sm: 'auto' } }}>
            Log sick day
          </Button>
        )}
      </Box>

      {filter && (
        <Typography variant="caption" color="text.secondary">
          Filtered by a tile. Click it again to show everyone.
        </Typography>
      )}

      {shown.length === 0 && view === 'cards' && !search && !filter ? (
        <Paper sx={{ p: 4, textAlign: 'center', color: 'text.secondary' }}>Nobody is on your team yet.</Paper>
      ) : view === 'table' ? (
        shown.length === 0 ? (
          <Paper sx={{ p: 4, textAlign: 'center', color: 'text.secondary' }}>Nobody matches this tile.</Paper>
        ) : (
          <MemberTable
            members={shown} sortKey={sortKey} sortDir={sortDir} onSort={sortBy}
            selectedId={selectedId} nextOffOf={nextOff} onOpen={openPerson}
          />
        )
      ) : (
        <>
          {/* Vacation left is a salaried figure; hourly staff have a counter, not a balance. */}
          {salariedShown.length > 0 && (
            <Panel title="Vacation left"><VacationRings members={salariedShown} colorOf={colorOf} labelOf={labelOf} /></Panel>
          )}
          {/* The grid: Salaried and Hourly bands when both are present, then the two
              dashed modules. A search fades out who no longer matches. */}
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fill, minmax(min(260px, 100%), 1fr))', position: 'relative' }}>
            <AnimatePresence mode="popLayout" initial={false}>
              {salariedShown.length > 0 && hourlyShown.length > 0
                ? [
                    <GroupBanner key="band-salaried" label="Salaried" count={salariedShown.length} />,
                    ...salariedShown.map((mem, k) => renderMemberCard(mem, k)),
                    <GroupBanner key="band-hourly" label="Hourly" count={hourlyShown.length} />,
                    ...hourlyShown.map((mem, k) => renderMemberCard(mem, k)),
                  ]
                : shown.map((mem, k) => renderMemberCard(mem, k))}
              {/* The dashed "add a new hire" card, unchanged from the old look. */}
              <m.div key="add-new-hire" layout style={{ display: 'grid' }}>
                <Button variant="outlined" onClick={onAddNewHire} sx={{ minHeight: 180, borderStyle: 'dashed', borderRadius: `${theme.shape.borderRadius}px` }}>
                  + Add a new hire
                </Button>
              </m.div>
              {/* "Add to my team" as a dashed card that turns into an inline module. */}
              <m.div key="add-to-team" layout style={{ display: 'grid' }}>
                {adding ? (
                  <AddToTeamCard processingEnabled={processingEnabled} onAdd={doAdd} onClose={() => setAdding(false)} />
                ) : (
                  <Tooltip title={processingEnabled ? '' : 'Adding is off while the system is in reporting-only mode.'}>
                    <span style={{ display: 'grid' }}>
                      <Button variant="outlined" disabled={!processingEnabled} onClick={() => setAdding(true)} sx={{ minHeight: 180, borderStyle: 'dashed', borderRadius: `${theme.shape.borderRadius}px`, width: '100%' }}>
                        + Add to my team
                      </Button>
                    </span>
                  </Tooltip>
                )}
              </m.div>
            </AnimatePresence>
          </Box>
        </>
      )}
    </>
  );
}

/**
 * A full-width banner heading one group of team cards in the balances grid.
 *
 * @param props.label - "Salaried" or "Hourly".
 * @param props.count - How many people are in the group.
 * @returns The banner, spanning every grid column.
 */
function GroupBanner({ label, count }: { label: string; count: number }) {
  return (
    <Box sx={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'baseline', gap: 1, pt: 0.5 }}>
      <Typography sx={{ fontWeight: 700 }}>{label}</Typography>
      <Typography variant="caption" color="text.secondary">{count} {count === 1 ? 'person' : 'people'}</Typography>
    </Box>
  );
}

/**
 * The inline "add to my team" module (#372): the dashed card becomes a search
 * over the addable staff; once a person is picked it shows their name, placement
 * and current managers, with a confirm action. Escape or a click away closes it.
 *
 * @param props.processingEnabled - Whether adding is on (the confirm is disabled otherwise).
 * @param props.onAdd - Add the picked person (their id and name).
 * @param props.onClose - Close the module without adding.
 * @returns The inline module card.
 */
function AddToTeamCard({ processingEnabled, onAdd, onClose }: {
  processingEnabled: boolean; onAdd: (id: string, name: string) => void; onClose: () => void;
}) {
  const [options, setOptions] = useState<AddableMember[]>([]);   // the addable staff
  const [picked, setPicked] = useState<AddableMember | null>(null);  // the chosen person
  useEffect(() => {
    let live = true;
    getAddableMembers().then((r) => { if (live) setOptions(r.data.people || []); }).catch(() => { if (live) setOptions([]); });
    return () => { live = false; };
  }, []);
  return (
    // Click away closes; disablePortal keeps the options inside this subtree so a
    // click on an option is not read as "away".
    <ClickAwayListener onClickAway={onClose}>
      <Paper
        onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }}
        sx={{ p: 2, display: 'grid', gap: 1.25, border: '1px dashed', borderColor: 'primary.main', minHeight: 180, alignContent: 'start' }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <Typography sx={{ fontWeight: 700 }}>Add to my team</Typography>
          <IconButton size="small" aria-label="Close" onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
        </Box>
        <Autocomplete
          openOnFocus disablePortal size="small" options={options} value={picked}
          getOptionLabel={(o) => o.name}
          isOptionEqualToValue={(a, b) => a.id === b.id}
          onChange={(_, p) => setPicked(p)}
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
        {picked && (
          <Box sx={{ display: 'grid', gap: 0.25 }}>
            <Typography sx={{ fontWeight: 700, overflowWrap: 'anywhere' }}>{picked.name}</Typography>
            <Typography variant="caption" color="text.secondary">{picked.department || 'No department'} · {picked.location || 'No location'}</Typography>
            <Typography variant="caption" color="text.secondary">
              Managers: {picked.managers && picked.managers.length ? picked.managers.join(', ') : 'None'}
            </Typography>
          </Box>
        )}
        <Box sx={{ display: 'flex', gap: 1, justifyContent: 'flex-end', mt: 'auto' }}>
          <Button size="small" onClick={onClose}>Cancel</Button>
          <Button size="small" variant="contained" disabled={!picked || !processingEnabled} onClick={() => picked && onAdd(picked.id, picked.name)}>
            Add
          </Button>
        </Box>
      </Paper>
    </ClickAwayListener>
  );
}

/**
 * The team as a sortable table that never scrolls sideways: it drops the Make-Up
 * and next-off columns on a phone and wraps names instead of scrolling. A row
 * opens the person's profile.
 *
 * @param props.members - The people to list (already filtered and sorted).
 * @param props.sortKey - The active sort column.
 * @param props.sortDir - 1 ascending, -1 descending.
 * @param props.onSort - Sort by a column.
 * @param props.selectedId - The open person's id (for the row highlight).
 * @param props.nextOffOf - A person's next absence.
 * @param props.onOpen - Open a person's profile.
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
