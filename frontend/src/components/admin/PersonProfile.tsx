import { useMemo, useRef, useState } from 'react';
import {
  Autocomplete, Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, MenuItem, Stack, TextField,
  ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import AddIcon from '@mui/icons-material/Add';
import CloseIcon from '@mui/icons-material/Close';
import RemoveIcon from '@mui/icons-material/Remove';
import EditOutlinedIcon from '@mui/icons-material/EditOutlined';
import KindPill from '../KindPill';
import TakenFrom from '../TakenFrom';
import { STATUS_COLOR } from '../dataGridDefaults';
import { ManagerOption } from '../AddEmployee';
import { requestAmount, requestKind, requestWhat, requestWhen } from '../../utils/requestText';
import { Enter } from '../Motion';
import { StaffChange, StaffRecord, SaveStaffPayload, saveStaffRecord, archiveStaff } from '../../api/staff';
// Manager-mode writes (#372): the same team endpoints the Team tab already uses,
// so a manager's balance save and self-removal run through the guarded routes.
import { getTeamMemberManagers, removeTeamMember, updateTeamEntitlements } from '../../api/client';
import { formatDay, formatInstantDay } from '../../utils/dates';
import { isHourly } from '../../utils/employment';

/*
 * The admin person profile (#273, #276, #306). The staff row grows into this
 * panel in the table's place (no side drawer): the left column holds balance and
 * entitlement tiles as click-to-edit tiles and every Staff Directory detail as a
 * click-to-edit field; the right column holds one Activity list that merges the
 * person's requests and HR's hand edits, newest first, grouped by year. One save
 * bar covers every change and needs a reason.
 */

/** A balance pot: its wire key, SharePoint column, label, step and entitlement. */
interface Pot {
  /** snake_case wire key sent in the save's ``changes``. */
  key: string;
  /** The Staff Directory column it reads from. */
  col: string;
  /** Human label on the tile. */
  label: string;
  /** Unit line when the pot is not measured against an entitlement. */
  unit?: string;
  /** The entitlement pot this one fills (draws the meter). */
  of?: string;
  /** The - / + step size. */
  step: number;
  /** The lowest a grant may go (entitlements only). */
  min?: number;
}

/** The seven pots, in the mockup's order: two grants, then the five balances. */
const POTS: Pot[] = [
  { key: 'vacation_entitlement', col: 'DefaultYearlyVacationDays', label: 'Vacation entitlement', unit: 'days a year', step: 0.5, min: 0.5 },
  { key: 'sick_entitlement', col: 'SickDayEntitlement', label: 'Sick entitlement', unit: 'days a year', step: 0.5, min: 0.5 },
  { key: 'vacation', col: 'CurrentVacationBalance', label: 'Vacation', of: 'vacation_entitlement', step: 0.5 },
  { key: 'sick', col: 'CurrentSickDayBalance', label: 'Sick', of: 'sick_entitlement', step: 0.5 },
  { key: 'make_up', col: 'CurrentOvertimeBalance', label: 'Make-up', unit: 'days (hours ÷ 8)', step: 0.125 },
  { key: 'carry_over', col: 'CarryOver', label: 'Carry-over', unit: 'days', step: 0.5 },
  { key: 'payout', col: 'Payout', label: 'Payout', unit: 'days', step: 0.5 },
];
const POT_BY_KEY: Record<string, Pot> = Object.fromEntries(POTS.map((p) => [p.key, p]));

/** Location choices the app already knows (matches LOCATION_PROVINCE_MAP). */
const LOCATIONS = ['Barrie', 'British Columbia', 'Leaside', 'Newfound Land', 'Ottawa', 'Toronto Victoria Park', 'Toronto Warden'];
/** Department choices the mockup lists. */
const DEPARTMENTS = ['Door', 'Finance', 'Hardware', 'Project Management', 'Residential Showroom', 'Warehouse'];
/** Employment type choices the balance engine acts on. */
const SALARY_OPTS = ['Salary', 'Hourly', 'Hourly BC'];
/** The system-check gate's two states. */
const CHECK_OPTS = ['Ready', 'Editing'];

/** The editor kind a field uses. */
type FieldType = 'text' | 'email' | 'tel' | 'date' | 'area' | 'choice' | 'seg' | 'person' | 'managers';

/** One editable Staff Directory field: how it is labelled, typed and stored. */
interface FieldDef {
  label: string;
  type: FieldType;
  /** The SharePoint column for a text/choice/seg/date field. */
  col?: string;
  /** The choices for a choice/seg field. */
  opts?: string[];
  /** Span the whole row (comments). */
  wide?: boolean;
}

/** Every editable field keyed by its wire key; the save maps these to columns. */
const FIELDS: Record<string, FieldDef> = {
  name: { label: 'Name', type: 'text', col: 'Title' },
  email: { label: 'Email', type: 'email', col: 'EmailAddress' },
  cell: { label: 'Cell', type: 'tel', col: 'CellNumber' },
  extension: { label: 'Extension', type: 'tel', col: 'Extension' },
  birthday: { label: 'Birthday', type: 'date', col: 'Birthday' },
  location: { label: 'Location', type: 'choice', col: 'Location', opts: LOCATIONS },
  department: { label: 'Department', type: 'choice', col: 'Department', opts: DEPARTMENTS },
  salary_hourly: { label: 'Salary or hourly', type: 'seg', col: 'SalaryHourly', opts: SALARY_OPTS },
  supervisor_text: { label: 'Supervisor (text)', type: 'text', col: 'Supervisor' },
  system_check: { label: 'System check', type: 'seg', col: 'SystemCheck', opts: CHECK_OPTS },
  request_allow_date: { label: 'Can book vacation until', type: 'date', col: 'RequestAllowDate' },
  comments: { label: 'Comments', type: 'area', col: 'Comments', wide: true },
  title_link: { label: 'Person link', type: 'person' },
  supervisor_link: { label: 'Supervisor link', type: 'person' },
  managers: { label: 'Managers', type: 'managers' },
};

/** SharePoint column (as the history stores it) -> friendly label. */
const HISTORY_LABELS: Record<string, string> = {
  CurrentVacationBalance: 'Vacation', CurrentSickDayBalance: 'Sick', CurrentOvertimeBalance: 'Make-up',
  CarryOver: 'Carry-over', Payout: 'Payout', DefaultYearlyVacationDays: 'Vacation entitlement',
  SickDayEntitlement: 'Sick entitlement', Title: 'Name', EmailAddress: 'Email', CellNumber: 'Cell',
  Extension: 'Extension', Birthday: 'Birthday', Location: 'Location', Department: 'Department',
  SalaryHourly: 'Salary or hourly', Supervisor: 'Supervisor (text)', SystemCheck: 'System check',
  RequestAllowDate: 'Can book vacation until', Comments: 'Comments', TitleLink: 'Person link',
  SupervisorLink: 'Supervisor link', AllManagers: 'Managers', Archived: 'Archived',
};

/** A person-link edit value: the chosen user id (or null to clear) and the name. */
interface PersonEdit { sp_user_id: number | null; name: string }

/**
 * A balance as at most three decimals, trailing zeros trimmed.
 *
 * @param n - The value.
 * @returns The short string.
 */
function fmt(n: number): string {
  return String(Math.round(n * 1000) / 1000);
}

/** Whether two name lists hold the same people, ignoring order. */
function sameNames(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const set = new Set(a);
  return b.every((n) => set.has(n));
}

interface Props {
  /** The person's record from GET /admin/employees/{id} (admin) or GET
   *  /team/members/{id}/record (manager): the shape is identical (#372). */
  record: StaffRecord;
  /** Their requests, for the requests list. */
  requests: any[];
  /** SharePoint users for the person and manager pickers ({sp_user_id, name}).
   *  Unused in manager mode (the managers module is read-only there), so the
   *  Team tab passes an empty list. */
  users: ManagerOption[];
  /** Setup-check problems for this person, shown as badges. */
  setupProblems: string[];
  /** Whether writes are enabled; Save is off when processing is disabled. */
  processingEnabled: boolean;
  /** Go back to the staff table. */
  onBack: () => void;
  /** Called after a successful save so the parent reloads the record and row. */
  onSaved: () => void;
  /** Surface a message to the dashboard snackbar. */
  onNotify: (message: string, severity: 'success' | 'error') => void;
  /** Which screen this is (#372). 'admin' (default) is the staff screen, every
   *  detail click-to-edit with an Archive button. 'manager' is the manager
   *  dashboard's team person view: balances and entitlements stay editable (saved
   *  through the team route under the manager's name), the details are read-only,
   *  there is no Archive or rename, and the managers module only lets the calling
   *  manager remove themselves. */
  mode?: 'admin' | 'manager';
  /** Manager mode only: the signed-in manager's own display name, so their own
   *  chip in the managers module is the one that can be removed and so a view of
   *  their own record opens read-only (the team API refuses self edits). */
  managerName?: string;
  /** Manager mode only: called after the manager removes themselves from this
   *  person, so the Team tab returns to the grid. */
  onRemovedSelf?: () => void;
}

/**
 * The person profile: header, balance tiles, history, requests and every
 * editable Staff Directory detail, with one save bar for all of it. Shared by
 * the admin staff screen and, in manager mode, the manager dashboard's team
 * person view (#372).
 *
 * @param props - See {@link Props}.
 * @returns The profile body.
 */
export default function PersonProfile({
  record, requests, users, setupProblems, processingEnabled, onBack, onSaved, onNotify,
  mode = 'admin', managerName = '', onRemovedSelf,
}: Props) {
  const theme = useTheme();
  const t = theme.tokens;
  const manager = mode === 'manager';                            // the team person view (#372)
  const [edits, setEdits] = useState<Record<string, any>>({});   // wire key -> unsaved value
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);  // which field is open in its inline editor
  const [archiveOpen, setArchiveOpen] = useState(false);         // the archive confirmation dialog (#119)
  const [archiveReason, setArchiveReason] = useState('');        // required reason for the archive
  const [archiving, setArchiving] = useState(false);             // archive request in flight

  // The saved managers are {sp_user_id, name}; the editor works in names, so keep
  // a names list and fold their ids into the name -> id map used on save.
  const savedManagers = useMemo(() => record.people.managers || [], [record.people.managers]);
  const savedManagerNames = useMemo(() => savedManagers.map((m) => m.name), [savedManagers]);

  // name -> SharePoint user id, so a picked person resolves to a lookup id on
  // save; includes the saved managers so an existing one always resolves.
  const nameToId = useMemo(
    () => new Map<string, number>([
      ...users.map((u) => [u.name, u.sp_user_id] as [string, number]),
      ...savedManagers.map((m) => [m.name, m.sp_user_id] as [string, number]),
    ]),
    [users, savedManagers],
  );
  const peopleNames = useMemo(() => users.map((u) => u.name), [users]);

  /** The saved value of a pot (its SharePoint column as a number). @param p - The pot. */
  const potOriginal = (p: Pot) => Number(record.fields[p.col] ?? 0);
  /** The pot's value on screen (an edit, or the saved value). @param p - The pot. */
  const potValue = (p: Pot) => (edits[p.key] !== undefined ? Number(edits[p.key]) : potOriginal(p));

  /** The saved value of a field. @param key - Wire key. @returns Name string, name list, or text. */
  const fieldOriginal = (key: string): any => {
    if (key === 'managers') return savedManagerNames;              // names for the editor
    if (FIELDS[key].type === 'person') return (record.people as any)[key] || '';
    return String(record.fields[FIELDS[key].col as string] ?? '');
  };
  /** The field's value on screen. @param key - Wire key. */
  const fieldValue = (key: string): any => (edits[key] !== undefined ? edits[key] : fieldOriginal(key));

  /** Whether a new value matches the saved one, so a no-op edit is dropped. */
  const equalsOriginal = (key: string, value: any): boolean => {
    if (POT_BY_KEY[key]) return Number(value) === potOriginal(POT_BY_KEY[key]);
    if (key === 'managers') return sameNames(value as string[], fieldOriginal(key) as string[]);
    if (FIELDS[key].type === 'person') return ((value as PersonEdit)?.name || '') === fieldOriginal(key);
    // A dateTime column is compared on the day, so re-picking the same day is a no-op.
    if (FIELDS[key].type === 'date') return String(value).slice(0, 10) === String(fieldOriginal(key)).slice(0, 10);
    return String(value) === fieldOriginal(key);
  };

  /** Record or clear an edit; a value equal to the saved one clears it. */
  const setEdit = (key: string, value: any) => {
    setEdits((prev) => {
      const next = { ...prev };
      if (equalsOriginal(key, value)) delete next[key];
      else next[key] = value;
      return next;
    });
  };

  const changedKeys = Object.keys(edits);

  /** The label for a change-summary / history key. @param key - Wire key. */
  const labelOf = (key: string) => (POT_BY_KEY[key] ? POT_BY_KEY[key].label : FIELDS[key]?.label || key);

  /** A value written for the change summary. @param key - Wire key. @param value - The value. */
  const showValue = (key: string, value: any): string => {
    if (key === 'managers') return (value as string[]).length ? (value as string[]).join(', ') : 'None';
    if (FIELDS[key]?.type === 'person') return (value as PersonEdit)?.name || (typeof value === 'string' ? value : '') || 'Not set';
    if (typeof value === 'number') return fmt(value);
    return value === '' || value == null ? 'Not set' : String(value);
  };

  /**
   * Save every change in one request with the reason, then let the parent
   * reload. The pots go in ``changes``; the directory fields go in ``fields``,
   * person links and managers resolved to SharePoint user ids.
   */
  const save = async () => {
    if (!changedKeys.length || !reason.trim() || saving) return;
    if (manager) {
      // Manager mode (#372): only the balance pots are editable here (the details
      // are read-only and the managers module saves itself), so send just the pot
      // changes to the team entitlements route, which validates them exactly as
      // the admin save does and records them under the manager's name.
      const changes: Record<string, number> = {};                 // pot wire key -> new number
      for (const key of changedKeys) if (POT_BY_KEY[key]) changes[key] = Number(edits[key]);  // pots only
      setSaving(true);
      try {
        const res = await updateTeamEntitlements(record.employee_id, { changes, reason: reason.trim() });
        setEdits({});                                              // clear the staged edits
        setReason('');                                            // and the reason box
        setEditingKey(null);
        onNotify(res.data.notice || 'Saved.', res.data.notice ? 'error' : 'success');  // notice => a soft refusal
        onSaved();                                                // parent re-reads the record + team
      } catch (err: any) {
        const detail = err.response?.data?.detail;                // the route's plain message
        onNotify(typeof detail === 'string' ? detail : 'The change could not be saved.', 'error');
      } finally {
        setSaving(false);
      }
      return;                                                     // the admin path below never runs in manager mode
    }
    const payload: SaveStaffPayload = { reason: reason.trim(), changes: {}, fields: {} };
    for (const key of changedKeys) {
      if (POT_BY_KEY[key]) {
        payload.changes![key] = Number(edits[key]);              // numeric pot
      } else if (FIELDS[key].type === 'person') {
        const v = edits[key] as PersonEdit;
        payload.fields![key] = { sp_user_id: v.sp_user_id ?? null, name: v.name || '' };
      } else if (key === 'managers') {
        const names = edits[key] as string[];
        const ids = names.map((n) => nameToId.get(n)).filter((id): id is number => typeof id === 'number');
        payload.fields![key] = { ids, names };
      } else {
        payload.fields![key] = String(edits[key]);               // text / choice / date
      }
    }
    setSaving(true);
    try {
      const res = await saveStaffRecord(record.employee_id, payload);
      setEdits({});
      setReason('');
      setEditingKey(null);
      onNotify(res.data.notice || 'Saved to the Staff Directory.', res.data.notice ? 'error' : 'success');
      onSaved();                                                 // parent refetches record + row
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      onNotify(typeof detail === 'string' ? detail : 'The change could not be saved.', 'error');
    } finally {
      setSaving(false);
    }
  };

  /** Undo every unsaved change. */
  const cancel = () => { setEdits({}); setReason(''); setEditingKey(null); };

  // The person's still-pending requests, shown in the archive dialog so the admin
  // knows they stay pending (they are not auto-rejected) and an admin will still
  // need to handle them (#119).
  const pendingRequests = useMemo(
    () => requests.filter((r) => String(r.Status || '').toLowerCase() === 'pending'),
    [requests],
  );

  /** Archive this person: they drop out of every list and can no longer make
   *  requests, while their record stays. Needs a reason (#119). */
  const doArchive = async () => {
    if (!archiveReason.trim()) return;                         // a reason is required
    setArchiving(true);
    try {
      await archiveStaff(record.employee_id, archiveReason.trim());
      onNotify('The person was archived.', 'success');
      setArchiveOpen(false);
      setArchiveReason('');
      onSaved();                                               // refresh the dashboard's balances list
      onBack();                                                // return to the table; they are no longer listed
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      onNotify(typeof detail === 'string' ? detail : 'The person could not be archived.', 'error');
    } finally {
      setArchiving(false);
    }
  };

  // hourly (current or edited type): Sick is the only balance, vacation is a
  // counter, and Make-up/Carry-over/Payout do not apply (#300).
  const hourlyStaff = isHourly(String(fieldValue('salary_hourly')));
  // The record carries no vacation_taken, so rebuild the counter from this
  // person's own requests: approved Vacation days whose start date is this year
  // (matches app/services/employment.py vacation_days_counted).
  const currentYear = new Date().getFullYear();
  const vacationTaken = useMemo(
    () => requests
      .filter((r) => r.request_type === 'leave' && r.Status === 'Approved'
        && r.LeaveType === 'Vacation' && Number(String(r.StartDate || '').slice(0, 4)) === currentYear)
      .reduce((sum, r) => sum + (Number(r.Days) || 0), 0),
    [requests, currentYear],
  );
  // For hourly: the two entitlements and Sick only (vacation becomes the counter below).
  const potsToShow = hourlyStaff ? POTS.filter((p) => ['vacation_entitlement', 'sick_entitlement', 'sick'].includes(p.key)) : POTS;

  // Header values follow unsaved edits live.
  const currentName = String(fieldValue('name'));
  const initials = currentName.split(' ').map((w) => w[0] || '').join('').slice(0, 2).toUpperCase();
  const currentEmail = String(fieldValue('email'));
  const currentDept = String(fieldValue('department'));
  const currentLoc = String(fieldValue('location'));
  const currentMgrs = fieldValue('managers') as string[];
  // Manager mode: a manager viewing their own record edits nothing (the team API
  // refuses self edits), so the balance tiles and the self-removal are off (#372).
  // The name is read-only in manager mode, so currentName matches the record.
  const selfView = manager && currentName === managerName;

  return (
    <>
      <Box sx={{
        bgcolor: 'background.paper', border: 1, borderColor: 'divider', borderRadius: 3, overflow: 'hidden',
        // Leave room under the profile for the fixed save bar, so its last fields
        // are never hidden behind it while there are unsaved changes.
        mb: changedKeys.length > 0 ? { xs: 34, sm: 26 } : 0,
      }}>
        {/* Header: back, avatar, name (editable), email, placement, badges. */}
        <Box sx={{ display: 'flex', gap: 2, alignItems: 'center', p: 2.25, borderBottom: 1, borderColor: 'divider', flexWrap: 'wrap' }}>
          <IconButton aria-label="Back to staff" onClick={onBack} sx={{ border: 1, borderColor: t.line2, borderRadius: 2 }}>
            <ArrowBackIcon />
          </IconButton>
          <Box sx={{ width: 52, height: 52, borderRadius: 2, bgcolor: 'primary.main', color: 'primary.contrastText', display: 'grid', placeItems: 'center', fontFamily: t.display, fontSize: 22 }}>
            {initials}
          </Box>
          <Box sx={{ flex: 1, minWidth: 0 }}>
            {manager ? (
              // Manager mode: the name is read-only, no rename (#372).
              <Typography variant="h5" sx={{ overflowWrap: 'anywhere' }}>{currentName}</Typography>
            ) : editingKey === 'name' ? (
              <TextField
                autoFocus size="small" defaultValue={currentName} inputProps={{ 'aria-label': 'Name' }}
                onBlur={(e) => { setEdit('name', e.target.value.trim() || currentName); setEditingKey(null); }}
                onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLElement).blur(); if (e.key === 'Escape') setEditingKey(null); }}
                sx={{ maxWidth: 360 }}
              />
            ) : (
              <Typography
                variant="h5" tabIndex={0} role="button" title="Click to rename"
                onClick={() => setEditingKey('name')}
                onKeyDown={(e) => { if (e.key === 'Enter') setEditingKey('name'); }}
                sx={{ cursor: 'pointer', borderRadius: 1, px: 0.5, mx: -0.5, bgcolor: edits.name !== undefined ? alpha(t.accent, 0.18) : 'transparent', '&:hover': { bgcolor: t.hover } }}
              >
                {currentName}
              </Typography>
            )}
            <Stack direction="row" spacing={1.5} useFlexGap flexWrap="wrap" sx={{ mt: 0.25, color: 'text.secondary', fontSize: 13.5 }}>
              <Box
                component="button" type="button" title="Copy email"
                onClick={() => { navigator.clipboard?.writeText(currentEmail).then(() => onNotify('Email copied', 'success'), () => {}); }}
                sx={{ border: 0, background: 'none', p: 0, color: 'primary.main', cursor: 'pointer', fontWeight: 600, textDecoration: 'underline dotted', font: 'inherit' }}
              >
                {currentEmail || 'No email'}
              </Box>
              <span>{(currentDept || 'No department')} · {currentLoc || 'No location'}</span>
            </Stack>
          </Box>
          <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap" justifyContent="flex-end" alignItems="center">
            {fieldValue('salary_hourly') && <Chip size="small" label={String(fieldValue('salary_hourly'))} />}
            {!currentMgrs.length && <Chip size="small" color="error" variant="outlined" label="No manager" />}
            {fieldValue('system_check') === 'Editing' && <Chip size="small" color="warning" variant="outlined" label="System check: Editing" />}
            {setupProblems.map((p) => <Chip key={p} size="small" color="error" variant="outlined" label={p} />)}
            {/* Archive a departed person (#119): admin only. A manager has no
                archive power over their team, so the button is hidden there (#372). */}
            {!manager && (
              <Tooltip title={processingEnabled ? '' : 'Archiving is off while the system is in reporting-only mode.'}>
                <span>
                  <Button
                    size="small" color="error" variant="outlined"
                    disabled={!processingEnabled}
                    onClick={() => { setArchiveReason(''); setArchiveOpen(true); }}
                  >
                    Archive
                  </Button>
                </span>
              </Tooltip>
            )}
          </Stack>
        </Box>

        {/* Body: balances + every detail on the left, the one Activity list on the right (#306). */}
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'minmax(0, 1.25fr) minmax(0, 1fr)' } }}>
          <Box sx={{ p: 2.5, display: 'grid', gap: 2.5, alignContent: 'start', minWidth: 0 }}>
            <SectionHead title="Entitlements and balances" hint="Click a tile to change it" />
            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 1.25 }}>
              {potsToShow.flatMap((p) => {
                // hourly may set a 0 vacation entitlement, so drop the 0.5 floor and relabel its caption (#300).
                const pot = hourlyStaff && p.key === 'vacation_entitlement'
                  ? { ...p, min: 0, unit: 'hourly staff have no vacation entitlement.' }
                  : p;
                const tile = (
                  <Tile
                    key={p.key} pot={pot} value={potValue(p)} saved={potOriginal(p)}
                    entitlement={p.of ? potValue(POT_BY_KEY[p.of]) : undefined}
                    onChange={(v) => setEdit(p.key, v)}
                    // A manager's view of their own record is read-only (#372).
                    readOnly={selfView}
                  />
                );
                // For hourly, the vacation counter sits just before Sick, in place of the vacation balance tile.
                if (hourlyStaff && p.key === 'sick') {
                  return [
                    <CounterTile key="vacation-taken" label="Vacation taken" value={vacationTaken} caption="This year. Counted, not a balance." />,
                    tile,
                  ];
                }
                return [tile];
              })}
            </Box>

            {/* Details: moved under the tiles in the left column (#306 mockup). */}
            <SectionHead title="Details" hint="Click any field to change it" />
            <FieldGroup title="Contact">
              {['email', 'cell', 'extension', 'birthday'].map((k) => renderField(k))}
            </FieldGroup>
            <FieldGroup title="Placement">
              {['location', 'department', 'salary_hourly', 'supervisor_text', 'system_check', 'request_allow_date', 'comments'].map((k) => renderField(k))}
            </FieldGroup>
            <Box>
              <GroupTitle>Managers</GroupTitle>
              {manager ? (
                // Manager mode (#372): every manager is shown; only the calling
                // manager's own chip can be removed (that is "remove from my team").
                <ManagerChips
                  names={savedManagerNames} managerName={managerName}
                  employeeId={record.employee_id}
                  canRemove={!selfView && processingEnabled}
                  onNotify={onNotify}
                  onRemoved={() => onRemovedSelf?.()}
                />
              ) : (
                <ManagersEditor
                  names={currentMgrs} savedNames={fieldOriginal('managers') as string[]}
                  options={peopleNames.filter((n) => !currentMgrs.includes(n) && n !== currentName)}
                  onChange={(names) => setEdit('managers', names)}
                />
              )}
            </Box>
            <FieldGroup title="Microsoft 365">
              {['title_link', 'supervisor_link'].map((k) => renderField(k))}
            </FieldGroup>
            <FieldGroup title="Record">
              <Fact label="Staff Directory ID" value={record.employee_id} system />
              <Fact label="Last modified" value={formatInstantDay(String(record.fields.Modified ?? ''))} system />
            </FieldGroup>
          </Box>

          {/* Right column: requests and HR's hand edits in one timeline. */}
          <Box sx={{ p: 2.5, display: 'grid', gap: 2, alignContent: 'start', minWidth: 0, borderTop: { xs: 1, md: 0 }, borderLeft: { md: 1 }, borderColor: 'divider' }}>
            <SectionHead title="Activity" hint="Requests and changes, newest first" />
            <Activity requests={requests} history={record.history} />
          </Box>
        </Box>
      </Box>

      {/* Save bar: slides up when anything changed; Save needs a reason. */}
      {changedKeys.length > 0 && (
        <Box
          sx={{
            position: 'fixed', left: '50%', bottom: 16, transform: 'translateX(-50%)',
            width: 'min(760px, calc(100% - 32px))', bgcolor: 'background.paper', border: 1, borderColor: t.line2,
            borderRadius: 3, boxShadow: 6, p: 1.5, zIndex: (th) => th.zIndex.snackbar, display: 'grid', gap: 1.25,
          }}
          role="region" aria-label="Unsaved changes"
        >
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: '4px 12px', fontSize: 13.5, alignItems: 'baseline' }}>
            <Typography component="span" color="text.secondary" sx={{ fontSize: 13.5 }}>
              {changedKeys.length === 1 ? '1 change:' : `${changedKeys.length} changes:`}
            </Typography>
            {changedKeys.map((k) => (
              <Typography component="span" key={k} sx={{ fontSize: 13.5 }}>
                {labelOf(k)}{' '}
                <b style={{ fontVariantNumeric: 'tabular-nums' }}>
                  {showValue(k, POT_BY_KEY[k] ? potOriginal(POT_BY_KEY[k]) : fieldOriginal(k))} → {showValue(k, edits[k])}
                </b>
              </Typography>
            ))}
          </Box>
          <TextField
            size="small" value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="Reason, required. e.g. Day off Oct 6, approved verbally by Sam Supervisor"
            inputProps={{ 'aria-label': 'Reason for the change' }}
          />
          <Stack direction="row" spacing={1} justifyContent="flex-end">
            <Button onClick={cancel} disabled={saving}>Cancel</Button>
            <Tooltip title={processingEnabled ? '' : 'Saving is off while the system is in reporting-only mode.'}>
              <span>
                <Button variant="contained" onClick={save} disabled={saving || !reason.trim() || !processingEnabled}>
                  {saving ? 'Saving...' : 'Save'}
                </Button>
              </span>
            </Tooltip>
          </Stack>
        </Box>
      )}

      {/* Archive confirmation (#119): a required reason, and the pending requests
          that will stay pending so the admin knows to handle them. */}
      <Dialog open={archiveOpen} onClose={() => !archiving && setArchiveOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>Archive {currentName || 'this person'}?</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            They will drop out of every staff list, team, picker, report and approval route, and can no longer
            make requests. Their past requests and history stay on record, and you can restore them later.
          </Typography>
          {pendingRequests.length > 0 && (
            <Box sx={{ mb: 2, p: 1.5, borderRadius: 1.5, bgcolor: 'action.hover' }}>
              <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>
                {pendingRequests.length === 1 ? '1 request is still pending' : `${pendingRequests.length} requests are still pending`}
                {' '}and will stay pending:
              </Typography>
              <Stack component="ul" sx={{ m: 0, pl: 2.5 }} spacing={0.25}>
                {pendingRequests.slice(0, 8).map((r, i) => (
                  <Typography key={i} component="li" variant="caption" color="text.secondary">
                    {requestKind(r)} · {requestWhen(r)}
                  </Typography>
                ))}
              </Stack>
            </Box>
          )}
          <TextField
            autoFocus fullWidth size="small" value={archiveReason}
            onChange={(e) => setArchiveReason(e.target.value)}
            label="Reason (required)"
            placeholder="e.g. Left the company on Oct 31"
            inputProps={{ 'aria-label': 'Reason for archiving' }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setArchiveOpen(false)} disabled={archiving}>Cancel</Button>
          <Button color="error" variant="contained" onClick={doArchive} disabled={archiving || !archiveReason.trim()}>
            {archiving ? 'Archiving...' : 'Archive'}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );

  /** Render one detail field by its wire key. @param key - The field. */
  function renderField(key: string) {
    const f = FIELDS[key];
    const open = editingKey === key;
    const value = fieldValue(key);
    const dirty = edits[key] !== undefined;
    const display = f.type === 'person'
      ? ((value as PersonEdit)?.name || (typeof value === 'string' ? value : ''))
      : f.type === 'date'
        ? formatDay(String(value))                                // read as "Nov 2, 2026"; the editor still uses the bare day
        : String(value);

    if (manager) {
      // Manager mode (#372): details are read-only, so show the value with no
      // click-to-edit affordance and no inline editor.
      return (
        <Box key={key} sx={{ gridColumn: f.wide ? '1 / -1' : undefined, p: 0.75, mx: -0.75 }}>
          <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600, display: 'block' }}>{f.label}</Typography>
          <Typography sx={{ fontSize: 14.5, overflowWrap: 'anywhere', color: display ? 'text.primary' : 'text.secondary', fontStyle: display ? 'normal' : 'italic' }}>
            {display || 'Not set'}
          </Typography>
        </Box>
      );
    }

    return (
      <Box
        key={key}
        sx={{ gridColumn: f.wide ? '1 / -1' : undefined, borderRadius: 1.5, p: 0.75, mx: -0.75, bgcolor: dirty ? alpha(t.accent, 0.18) : 'transparent', '&:hover': open ? {} : { bgcolor: t.hover }, cursor: open ? 'default' : 'pointer' }}
        tabIndex={open ? -1 : 0} role="button" aria-label={`Change ${f.label}`}
        onClick={() => { if (!open) setEditingKey(key); }}
        onKeyDown={(e) => { if (!open && e.key === 'Enter') setEditingKey(key); }}
      >
        <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600, display: 'block' }}>{f.label}</Typography>
        {open ? (
          <FieldEditor
            field={f} value={value} people={peopleNames} nameToId={nameToId}
            onCommit={(v) => { setEdit(key, v); setEditingKey(null); }}
            onCancel={() => setEditingKey(null)}
          />
        ) : (
          <>
            <Typography sx={{ fontSize: 14.5, overflowWrap: 'anywhere', color: display ? 'text.primary' : 'text.secondary', fontStyle: display ? 'normal' : 'italic' }}>
              {display || 'Not set'}
            </Typography>
            {dirty && <Typography variant="caption" color="text.secondary" sx={{ textDecoration: 'line-through' }}>was {showValue(key, fieldOriginal(key))}</Typography>}
          </>
        )}
      </Box>
    );
  }
}

/** A section heading with an optional hint, matching the mockup. */
function SectionHead({ title, hint }: { title: string; hint?: string }) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1.25 }}>
      <Typography variant="h6" sx={{ flex: 1 }}>{title}</Typography>
      {hint && <Typography variant="caption" color="text.secondary">{hint}</Typography>}
    </Box>
  );
}

/** A small uppercase group label. */
function GroupTitle({ children }: { children: React.ReactNode }) {
  return (
    <Typography sx={{ fontSize: 12, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: 'text.secondary', mb: 1 }}>
      {children}
    </Typography>
  );
}

/** A labelled group of fact rows. */
function FieldGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Box>
      <GroupTitle>{title}</GroupTitle>
      <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '12px 16px' }}>{children}</Box>
    </Box>
  );
}

/** A read-only system fact (set by SharePoint), like the record id. */
function Fact({ label, value, system }: { label: string; value: string; system?: boolean }) {
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600, display: 'block' }}>
        {label}{system ? ' · set by SharePoint' : ''}
      </Typography>
      <Typography sx={{ fontSize: 14.5, overflowWrap: 'anywhere' }}>{value || 'Not set'}</Typography>
    </Box>
  );
}

/**
 * The inline editor for one field, by its type. Commits on Enter / blur / pick,
 * cancels on Escape.
 *
 * @param props.field - The field definition.
 * @param props.value - The current value (text, name, or PersonEdit).
 * @param props.people - Names for the person picker.
 * @param props.nameToId - Resolves a picked name to its SharePoint user id.
 * @param props.onCommit - Called with the new value.
 * @param props.onCancel - Called to drop the edit.
 * @returns The editor control.
 */
function FieldEditor({ field, value, people, nameToId, onCommit, onCancel }: {
  field: FieldDef; value: any; people: string[]; nameToId: Map<string, number>;
  onCommit: (v: any) => void; onCancel: () => void;
}) {
  if (field.type === 'seg') {
    return (
      <ToggleButtonGroup
        exclusive size="small" value={String(value)} sx={{ mt: 0.5, flexWrap: 'wrap' }}
        onChange={(_, v) => { if (v) onCommit(v); }}
      >
        {field.opts!.map((o) => <ToggleButton key={o} value={o}>{o}</ToggleButton>)}
      </ToggleButtonGroup>
    );
  }
  if (field.type === 'choice') {
    const opts = field.opts!.slice();
    if (value && !opts.includes(String(value))) opts.unshift(String(value));   // keep an off-list current value
    return (
      <TextField
        autoFocus select fullWidth size="small" defaultValue={String(value)} sx={{ mt: 0.5 }}
        inputProps={{ 'aria-label': field.label }}
        onChange={(e) => onCommit(e.target.value)}
      >
        {opts.map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
      </TextField>
    );
  }
  if (field.type === 'person') {
    const current = (value as PersonEdit)?.name || (typeof value === 'string' ? value : '');
    // disableClearable: a single person link cannot be cleared (set/change only),
    // so the clear "X" is not offered; a pick resolves its id from the name map.
    return (
      <Autocomplete
        openOnFocus disableClearable size="small" sx={{ mt: 0.5 }} options={people}
        value={(current || null) as string}
        onChange={(_, picked) => { if (picked) onCommit({ sp_user_id: nameToId.get(picked) ?? null, name: picked }); }}
        renderInput={(params) => <TextField {...params} autoFocus placeholder="Search Microsoft 365 people" aria-label={field.label} onKeyDown={(e) => { if (e.key === 'Escape') onCancel(); }} />}
      />
    );
  }
  // text, email, tel, date, area
  const isArea = field.type === 'area';
  // A date input needs a bare YYYY-MM-DD, so a stored timestamp is sliced to its day.
  const initial = field.type === 'date' ? String(value).slice(0, 10) : String(value);
  return (
    <TextField
      autoFocus fullWidth size="small" multiline={isArea} minRows={isArea ? 3 : undefined}
      type={field.type === 'area' ? undefined : field.type} defaultValue={initial} sx={{ mt: 0.5 }}
      inputProps={{ 'aria-label': field.label }}
      InputLabelProps={field.type === 'date' ? { shrink: true } : undefined}
      onBlur={(e) => onCommit(e.target.value.trim())}
      onKeyDown={(e) => {
        if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
        if (e.key === 'Enter' && !isArea) (e.target as HTMLElement).blur();
      }}
    />
  );
}

/**
 * The managers editor: current managers as chips (removable), added ones
 * highlighted, removed ones struck through, and an add box over the people list.
 *
 * @param props.names - The current manager names on screen.
 * @param props.savedNames - The saved manager names (to mark added/removed).
 * @param props.options - People who can be added.
 * @param props.onChange - Called with the new name list.
 * @returns The managers control.
 */
function ManagersEditor({ names, savedNames, options, onChange }: {
  names: string[]; savedNames: string[]; options: string[]; onChange: (names: string[]) => void;
}) {
  const theme = useTheme();
  const [adding, setAdding] = useState(false);
  const removed = savedNames.filter((n) => !names.includes(n));
  return (
    <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap" alignItems="center">
      {!names.length && !adding && <Chip size="small" color="error" variant="outlined" label="No manager" />}
      {names.map((n) => (
        <Chip
          key={n} label={n} size="small" onDelete={() => onChange(names.filter((x) => x !== n))}
          deleteIcon={<CloseIcon />}
          sx={!savedNames.includes(n) ? { bgcolor: alpha(theme.tokens.accent, 0.18), borderColor: theme.tokens.accent } : undefined}
          variant="outlined"
        />
      ))}
      {removed.map((n) => (
        <Chip key={`rm-${n}`} label={n} size="small" variant="outlined" sx={{ textDecoration: 'line-through', color: 'text.secondary', borderStyle: 'dashed' }} />
      ))}
      {adding ? (
        <Autocomplete
          openOnFocus size="small" options={options} sx={{ minWidth: 220 }}
          onChange={(_, picked) => { if (picked) onChange([...names, picked]); setAdding(false); }}
          onBlur={() => setAdding(false)}
          renderInput={(params) => <TextField {...params} autoFocus placeholder="Search Microsoft 365 people" aria-label="Add manager" />}
        />
      ) : (
        <Button size="small" variant="outlined" startIcon={<AddIcon />} onClick={() => setAdding(true)}>Add manager</Button>
      )}
    </Stack>
  );
}

/**
 * The managers module in manager mode (#372): every manager shown as a chip. The
 * calling manager's own chip is the only removable one (that is "remove from my
 * team"); deleting it opens an inline confirmation that warns when they are the
 * person's only manager, then calls the guarded team remove route. Other
 * managers' chips are read-only.
 *
 * @param props.names - The person's manager names (saved, read-only here).
 * @param props.managerName - The calling manager's own name, so their chip is found.
 * @param props.employeeId - Staff Directory id of the person being viewed.
 * @param props.canRemove - Whether self-removal is offered (off for a self view
 *   and while processing is disabled).
 * @param props.onNotify - Surface a message.
 * @param props.onRemoved - Called after a successful removal (the Team tab goes back).
 * @returns The managers chips and the inline remove confirmation.
 */
function ManagerChips({ names, managerName, employeeId, canRemove, onNotify, onRemoved }: {
  names: string[]; managerName: string; employeeId: string; canRemove: boolean;
  onNotify: (m: string, s: 'success' | 'error') => void; onRemoved: () => void;
}) {
  const theme = useTheme();
  const [confirming, setConfirming] = useState(false);       // the inline confirm box is open
  const [otherManager, setOtherManager] = useState<boolean | null>(null);  // would another manager remain
  const [busy, setBusy] = useState(false);                   // the remove call is in flight

  /** Open the confirmation, first reading whether anyone else manages them. */
  const openConfirm = async () => {
    setConfirming(true);                                     // show the box at once
    setOtherManager(null);                                   // "checking..." until the read lands
    try {
      const res = await getTeamMemberManagers(employeeId);   // the warning needs has_other_manager
      setOtherManager(Boolean(res.data.has_other_manager));  // true => someone else stays
    } catch {
      setOtherManager(null);                                 // unknown: the copy stays cautious
    }
  };

  /** Remove the calling manager from this person, then hand back to the grid. */
  const doRemove = async () => {
    setBusy(true);
    try {
      await removeTeamMember(employeeId);                    // DELETE /team/members/{id}
      onNotify('Removed from your team.', 'success');
      onRemoved();                                           // refresh the team and go back
    } catch (err: any) {
      onNotify(err.response?.data?.detail || 'Could not remove them from your team.', 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box sx={{ display: 'grid', gap: 1 }}>
      <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap" alignItems="center">
        {!names.length && <Chip size="small" color="error" variant="outlined" label="No manager" />}
        {names.map((n) => {
          const mine = n === managerName;                    // the calling manager's own chip
          return (
            <Chip
              key={n} label={mine ? `${n} (you)` : n} size="small" variant="outlined"
              // Only my own chip deletes, and only when removal is allowed; the
              // rest are plain read-only chips (#372).
              onDelete={mine && canRemove ? openConfirm : undefined}
              deleteIcon={<CloseIcon />}
            />
          );
        })}
      </Stack>
      {confirming && (
        <Box sx={{ display: 'grid', gap: 1, p: 1.5, borderRadius: 2, border: 1, borderColor: 'error.main', bgcolor: alpha(theme.palette.error.main, 0.08) }}>
          <Typography sx={{ fontWeight: 700 }}>Remove yourself from this person's team?</Typography>
          <Typography variant="body2" color="text.secondary">
            {otherManager === null
              ? 'Checking whether anyone else manages them...'
              : otherManager
                ? 'Another manager stays on them, so their requests still have somewhere to go.'
                : 'You are their only manager. Their requests will have nobody to go to until an admin or another manager adds them.'}
          </Typography>
          <Box sx={{ display: 'flex', gap: 1, justifyContent: 'flex-end' }}>
            <Button size="small" onClick={() => setConfirming(false)} disabled={busy}>Keep</Button>
            <Button size="small" color="error" variant="contained" onClick={doRemove} disabled={busy}>
              {busy ? 'Removing...' : 'Remove'}
            </Button>
          </Box>
        </Box>
      )}
    </Box>
  );
}

/**
 * One balance tile: the label, the value and (for a balance) a meter against its
 * entitlement. Click opens an inline editor with - / + steps or a typed value.
 *
 * @param props.pot - The pot definition.
 * @param props.value - Its value on screen.
 * @param props.saved - Its saved value, for the change delta.
 * @param props.entitlement - The entitlement total, for the meter.
 * @param props.onChange - Called with the new number.
 * @returns The tile.
 */
function Tile({ pot, value, saved, entitlement, onChange, readOnly = false }: {
  pot: Pot; value: number; saved: number; entitlement?: number; onChange: (v: number) => void;
  /** When true the tile only displays: no click-to-edit, no - / + (#372). */
  readOnly?: boolean;
}) {
  const theme = useTheme();
  const t = theme.tokens;
  const [editing, setEditing] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);            // so - / + also move the field's text
  const dirty = value !== saved;
  const negative = value < 0;
  const step = (delta: number) => {
    let v = Math.round((value + delta) * 1000) / 1000;
    if (pot.min != null && v < pot.min) v = pot.min;
    if (inputRef.current) inputRef.current.value = fmt(v);    // keep the open editor in step
    onChange(v);
  };
  const meterPct = entitlement ? Math.max(4, Math.min(100, (Math.abs(value) / (entitlement || 1)) * 100)) : 0;

  return (
    <Box
      // readOnly: no focus, no click handler, no pointer cursor (#372).
      tabIndex={editing || readOnly ? -1 : 0} role="button" aria-label={readOnly ? pot.label : `Change ${pot.label}`}
      onClick={() => !editing && !readOnly && setEditing(true)}
      onKeyDown={(e) => { if (!editing && !readOnly && e.key === 'Enter') setEditing(true); }}
      sx={{
        position: 'relative', border: 1, borderRadius: 2, p: 1.5, display: 'grid', gap: 0.5, textAlign: 'left',
        cursor: editing || readOnly ? 'default' : 'pointer',
        borderColor: editing ? 'primary.main' : dirty ? t.accent : 'divider',
        bgcolor: dirty && !editing ? alpha(t.accent, 0.14) : t.bg,
        boxShadow: editing ? `0 0 0 3px ${t.primarySoft}` : 'none',
      }}
    >
      {dirty && (
        <Box sx={{ position: 'absolute', top: 8, right: 8, fontSize: 11.5, fontWeight: 700, px: 0.75, borderRadius: 999, bgcolor: t.accent, color: 'background.paper' }}>
          {value - saved > 0 ? '+' : ''}{fmt(value - saved)}
        </Box>
      )}
      <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>{pot.label}</Typography>
      {editing ? (
        <Stack direction="row" spacing={0.5} alignItems="center" onClick={(e) => e.stopPropagation()}>
          <IconButton size="small" aria-label="Less" onClick={() => step(-pot.step)} sx={{ border: 1, borderColor: t.line2 }}><RemoveIcon fontSize="small" /></IconButton>
          <TextField
            autoFocus inputRef={inputRef} size="small" type="number" defaultValue={fmt(value)} inputProps={{ step: pot.step, min: pot.min, 'aria-label': `New ${pot.label}`, style: { textAlign: 'center', fontVariantNumeric: 'tabular-nums' } }}
            onChange={(e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v)) onChange(v); }}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === 'Escape') setEditing(false); }}
            onBlur={() => setEditing(false)}
          />
          <IconButton size="small" aria-label="More" onClick={() => step(pot.step)} sx={{ border: 1, borderColor: t.line2 }}><AddIcon fontSize="small" /></IconButton>
        </Stack>
      ) : (
        <Typography sx={{ fontSize: 28, fontWeight: 700, fontVariantNumeric: 'tabular-nums', lineHeight: 1.1, color: negative ? 'error.main' : 'text.primary' }}>{fmt(value)}</Typography>
      )}
      <Typography variant="caption" color="text.secondary">
        {pot.of ? `of ${fmt(entitlement || 0)} a year` : pot.unit}
      </Typography>
      {pot.of && (
        <Box sx={{ height: 4, borderRadius: 2, bgcolor: t.line, overflow: 'hidden' }}>
          <Box sx={{ height: '100%', width: `${meterPct}%`, borderRadius: 2, bgcolor: negative ? 'error.main' : 'primary.main', transition: 'width .4s ease' }} />
        </Box>
      )}
    </Box>
  );
}

/**
 * A read-only counter tile (#300): an hourly person's "vacation taken this
 * year", shown as days with no meter and no edit, so it never reads as a
 * balance. It sits where the vacation balance tile would be.
 *
 * @param props.label - The tile heading ("Vacation taken").
 * @param props.value - The counted days.
 * @param props.caption - The line saying it is a counter.
 * @returns The tile.
 */
function CounterTile({ label, value, caption }: { label: string; value: number; caption: string }) {
  const theme = useTheme();
  const t = theme.tokens;
  return (
    <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 2, p: 1.5, display: 'grid', gap: 0.5, bgcolor: t.bg }}>
      <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
      <Typography sx={{ fontSize: 28, fontWeight: 700, fontVariantNumeric: 'tabular-nums', lineHeight: 1.1 }}>
        {fmt(value)}
        {/* "days" is small beside the figure: this counts up, it is not a balance. */}
        <Box component="span" sx={{ fontSize: 14, fontWeight: 600, color: 'text.secondary', ml: 0.5 }}>days</Box>
      </Typography>
      <Typography variant="caption" color="text.secondary">{caption}</Typography>
    </Box>
  );
}

/** One item in the merged Activity timeline: a request, or an HR hand edit. */
type ActItem =
  | { kind: 'req'; key: string; day: number; year: number; r: any }        // a request row (carries taken_from)
  | { kind: 'chg'; key: string; day: number; year: number; e: StaffChange }; // an HR hand edit

/** The Activity filters, each a test over a merged item. */
const ACT_FILTERS: { key: string; label: string; test: (it: ActItem) => boolean }[] = [
  { key: 'all', label: 'All', test: () => true },                                                  // everything
  { key: 'leave', label: 'Leave', test: (it) => it.kind === 'req' && it.r.request_type === 'leave' },       // leave requests
  { key: 'mu', label: 'Make-up', test: (it) => it.kind === 'req' && it.r.request_type === 'overtime' },     // overtime requests
  { key: 'pending', label: 'Pending', test: (it) => it.kind === 'req' && (it.r.Status || 'Pending') === 'Pending' }, // not yet decided
  { key: 'changes', label: 'Changes', test: (it) => it.kind === 'chg' },                           // HR hand edits only
];

/** Rows a year shows before the "Show N more" control. */
const PER_YEAR = 5;

/**
 * A sortable YYYYMMDD number from a date or timestamp string's date part.
 *
 * @param iso - "YYYY-MM-DD" or an ISO timestamp (or '').
 * @returns y*10000 + m*100 + d, or 0 when unreadable.
 */
function dayNum(iso?: string | null): number {
  if (!iso) return 0;                                            // nothing to read
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);    // date part only, no timezone shift
  return y && m && d ? y * 10000 + m * 100 + d : 0;             // 0 when any part is missing
}

/**
 * The day a request sits on in the timeline: its approval day once decided, else
 * when it was created, else its start date.
 *
 * @param r - The raw request row.
 * @returns The date string used for sorting and the year group.
 */
function requestDay(r: any): string {
  return String(r.ApprovedDate || r.Created || r.StartDate || '');  // decided day wins, then created, then start
}

/**
 * The merged Activity list (#306): the person's requests and HR's hand edits in
 * one timeline, newest first, grouped by year. The current year is open; older
 * years collapse behind a header with a count, and each year shows five rows
 * before a "Show N more" control. Requests show the type pill, when, amount,
 * status and the "Taken from" chips; hand edits show a pencil marker, a muted
 * tint, a dashed edge and before to after with no status, so the two read apart
 * at a glance. Everything stacks, so a phone never scrolls sideways.
 *
 * @param props.requests - The person's raw request rows (each carries taken_from).
 * @param props.history - HR's hand edits, newest first (from the record).
 * @returns The Activity list.
 */
function Activity({ requests, history }: { requests: any[]; history: StaffChange[] }) {
  const [filter, setFilter] = useState('all');
  const [yearOpen, setYearOpen] = useState<Record<number, boolean>>({});   // per-year unfold override
  const [yearAll, setYearAll] = useState<Record<number, boolean>>({});     // per-year show-every-row override
  const currentYear = new Date().getFullYear();                            // the year open by default

  // Merge both kinds into one list: changes seeded first, then a stable descending
  // sort by day keeps a change above a request on the same day.
  const items = useMemo<ActItem[]>(() => {
    const chgs: ActItem[] = history.map((e, i): ActItem => {
      const day = dayNum(e.changed_at);                          // the day the edit was made
      return { kind: 'chg', key: `chg-${i}`, day, year: Math.floor(day / 10000), e };
    });
    const reqs: ActItem[] = requests.map((r): ActItem => {
      const day = dayNum(requestDay(r));                         // approval/created/start day
      return { kind: 'req', key: `req-${r.request_type}-${r.id}`, day, year: Math.floor(day / 10000), r };
    });
    return [...chgs, ...reqs].sort((a, b) => b.day - a.day);     // newest first, stable within a day
  }, [requests, history]);

  // Live counts per filter, so each chip carries its number.
  const counts = useMemo(
    () => ACT_FILTERS.reduce<Record<string, number>>((acc, f) => { acc[f.key] = items.filter(f.test).length; return acc; }, {}),
    [items],
  );

  const test = (ACT_FILTERS.find((f) => f.key === filter) || ACT_FILTERS[0]).test;   // the active filter's test
  const shown = items.filter(test);

  // Group the shown items by year, years newest first.
  const byYear = new Map<number, ActItem[]>();
  shown.forEach((it) => { const arr = byYear.get(it.year) || []; arr.push(it); byYear.set(it.year, arr); });
  const years = Array.from(byYear.keys()).sort((a, b) => b - a);

  return (
    <>
      {/* Filter chips with live counts (All / Leave / Make-up / Pending / Changes). */}
      <ToggleButtonGroup
        exclusive size="small" value={filter} onChange={(_, v) => v && setFilter(v)}
        aria-label="Filter activity" sx={{ flexWrap: 'wrap' }}
      >
        {ACT_FILTERS.map((f) => (
          <ToggleButton key={f.key} value={f.key} aria-label={`${f.label}, ${counts[f.key]}`}>
            {f.label}
            <Box component="span" sx={{ ml: 0.5, fontSize: 11, fontWeight: 700, opacity: 0.7, fontVariantNumeric: 'tabular-nums' }}>{counts[f.key]}</Box>
          </ToggleButton>
        ))}
      </ToggleButtonGroup>
      {/* Legend: what the pencil-marked rows are. */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, color: 'text.secondary', fontSize: 12.5 }}>
        <EditOutlinedIcon sx={{ fontSize: 15 }} /> a change made by HR, not a request
      </Box>

      <Enter key={filter}>
        {shown.length === 0 ? (
          <Typography color="text.secondary">Nothing here.</Typography>
        ) : (
          <Box sx={{ display: 'grid', minWidth: 0 }}>
            {years.map((year) => {
              const its = byYear.get(year)!;                      // this year's items
              const open = yearOpen[year] ?? year === currentYear; // current year open, older years folded
              const nr = its.filter((it) => it.kind === 'req').length;   // requests this year
              const nc = its.length - nr;                         // changes this year
              const summary = [nr ? `${nr} request${nr === 1 ? '' : 's'}` : '', nc ? `${nc} change${nc === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ');
              const visible = yearAll[year] ? its : its.slice(0, PER_YEAR);   // paged to five unless opened
              return (
                <Box key={year}>
                  {/* Year header: a button, so an older year folds into one summary line with a count. */}
                  <Box
                    component="button" type="button" aria-expanded={open}
                    onClick={() => setYearOpen((p) => ({ ...p, [year]: !open }))}
                    sx={{ width: '100%', display: 'flex', alignItems: 'center', gap: 1, border: 0, borderBottom: 1, borderColor: 'divider', background: 'none', color: 'text.secondary', font: 'inherit', cursor: 'pointer', py: 1, px: 0, textAlign: 'left', '&:hover': { color: 'text.primary' } }}
                  >
                    <Box component="span" sx={{ fontSize: 17, lineHeight: 1, transition: 'transform .2s', transform: open ? 'rotate(90deg)' : 'none' }}>›</Box>
                    <Box component="span" sx={{ fontWeight: 700 }}>{year || 'Undated'}</Box>
                    {summary && <Box component="span" sx={{ fontWeight: 600 }}>· {summary}</Box>}
                  </Box>
                  {open && (
                    <>
                      {visible.map((it) => (it.kind === 'req'
                        ? <RequestRow key={it.key} r={it.r} />
                        : <ChangeRow key={it.key} e={it.e} />))}
                      {its.length > PER_YEAR && (
                        <Button
                          size="small" variant="text" sx={{ justifySelf: 'start', mt: 0.5 }}
                          onClick={() => setYearAll((p) => ({ ...p, [year]: !yearAll[year] }))}
                        >
                          {yearAll[year] ? 'Show fewer' : `Show ${its.length - PER_YEAR} more`}
                        </Button>
                      )}
                    </>
                  )}
                </Box>
              );
            })}
          </Box>
        )}
      </Enter>
    </>
  );
}

/**
 * One request in the Activity list: the type pill, when, amount and status on a
 * wrapping line, then the "Taken from" chips on their own line below. Nothing is
 * fixed-width, so the row wraps instead of scrolling sideways on a phone.
 *
 * @param props.r - The raw request row.
 * @returns The request row.
 */
function RequestRow({ r }: { r: any }) {
  const status = r.Status || 'Pending';                          // SharePoint leaves it blank while pending
  const amount = requestAmount(r);                               // "3 days" / "4 hours" / ''
  return (
    <Box sx={{ display: 'grid', gap: 0.75, py: 1, borderBottom: 1, borderColor: 'divider', minWidth: 0 }}>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', minWidth: 0 }}>
        <KindPill kind={requestKind(r)} label={requestWhat(r)} />
        <Typography variant="body2" color="text.secondary" sx={{ flex: '1 1 auto', minWidth: 0 }}>{requestWhen(r)}</Typography>
        {amount && <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>{amount}</Typography>}
        <Chip size="small" color={STATUS_COLOR[status] || 'default'} label={status} />
      </Box>
      <TakenFrom value={r.taken_from} compact />
    </Box>
  );
}

/**
 * One HR hand edit in the Activity list: a pencil marker, a muted tint and a
 * dashed left edge set it apart from a request; it names the field and before to
 * after, then who, when and why, and never shows a status.
 *
 * @param props.e - The change row.
 * @returns The change row.
 */
function ChangeRow({ e }: { e: StaffChange }) {
  const theme = useTheme();
  const t = theme.tokens;
  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: 'auto minmax(0, 1fr)', columnGap: 1.25, rowGap: 0.25, py: 1, px: 1.25, borderBottom: 1, borderColor: 'divider', borderLeft: `2px dashed ${t.line2}`, bgcolor: t.hover, minWidth: 0 }}>
      <EditOutlinedIcon sx={{ fontSize: 16, color: 'text.secondary', mt: '2px' }} />
      <Typography sx={{ fontSize: 14, minWidth: 0, overflowWrap: 'anywhere' }}>
        {HISTORY_LABELS[e.field] || e.field}{' '}
        <b style={{ fontVariantNumeric: 'tabular-nums' }}>{String(e.old_value ?? '')} → {String(e.new_value ?? '')}</b>
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ gridColumn: 2, minWidth: 0, overflowWrap: 'anywhere' }}>
        Changed by HR · {formatInstantDay(e.changed_at)} · {e.reason}
      </Typography>
    </Box>
  );
}
