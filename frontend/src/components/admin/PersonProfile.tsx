import { useMemo, useRef, useState } from 'react';
import {
  Autocomplete, Box, Button, Chip, IconButton, MenuItem, Stack, TextField,
  ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import AddIcon from '@mui/icons-material/Add';
import CloseIcon from '@mui/icons-material/Close';
import RemoveIcon from '@mui/icons-material/Remove';
import KindPill from '../KindPill';
import { STATUS_COLOR } from '../dataGridDefaults';
import { ManagerOption } from '../AddEmployee';
import { requestKind, requestWhat, requestWhen } from '../../utils/requestText';
import { Enter } from '../Motion';
import { StaffRecord, SaveStaffPayload, saveStaffRecord } from '../../api/staff';

/*
 * The admin person profile (#273, #276). The staff row grows into this panel in
 * the table's place (no side drawer): balances and entitlements as click-to-edit
 * tiles, every Staff Directory detail as a click-to-edit field, managers, and
 * the change history. One save bar covers every change and needs a reason.
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
  /** Allow a typed "other" value (location). */
  other?: boolean;
  /** Span the whole row (comments). */
  wide?: boolean;
}

/** Every editable field keyed by its wire key; the save maps these to columns. */
const FIELDS: Record<string, FieldDef> = {
  name: { label: 'Name', type: 'text', col: 'Title' },
  email: { label: 'Email', type: 'email', col: 'EmailAddress' },
  cell: { label: 'Cell', type: 'tel', col: 'CellNumber' },
  extension: { label: 'Extension', type: 'text', col: 'Extension' },
  birthday: { label: 'Birthday', type: 'text', col: 'Birthday' },
  location: { label: 'Location', type: 'choice', col: 'Location', opts: LOCATIONS, other: true },
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
  SupervisorLink: 'Supervisor link', AllManagers: 'Managers',
};

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

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

/**
 * "Oct 14, 2026" from an ISO timestamp, in the reader's locale day.
 *
 * @param iso - The ISO 8601 string.
 * @returns The short date, or the raw string when unreadable.
 */
function niceDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
}

/** Whether two name lists hold the same people, ignoring order. */
function sameNames(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const set = new Set(a);
  return b.every((n) => set.has(n));
}

interface Props {
  /** The person's record from GET /admin/employees/{id}. */
  record: StaffRecord;
  /** Their requests, for the requests list. */
  requests: any[];
  /** SharePoint users for the person and manager pickers ({sp_user_id, name}). */
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
}

/**
 * The person profile: header, balance tiles, history, requests and every
 * editable Staff Directory detail, with one save bar for all of it.
 *
 * @param props - See {@link Props}.
 * @returns The profile body.
 */
export default function PersonProfile({ record, requests, users, setupProblems, processingEnabled, onBack, onSaved, onNotify }: Props) {
  const theme = useTheme();
  const t = theme.tokens;
  const [edits, setEdits] = useState<Record<string, any>>({});   // wire key -> unsaved value
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [reqFilter, setReqFilter] = useState('all');
  const [editingKey, setEditingKey] = useState<string | null>(null);  // which field is open in its inline editor

  // name -> SharePoint user id, so a picked person resolves to a lookup id on save.
  const nameToId = useMemo(() => new Map(users.map((u) => [u.name, u.sp_user_id])), [users]);
  const peopleNames = useMemo(() => users.map((u) => u.name), [users]);

  /** The saved value of a pot (its SharePoint column as a number). @param p - The pot. */
  const potOriginal = (p: Pot) => Number(record.fields[p.col] ?? 0);
  /** The pot's value on screen (an edit, or the saved value). @param p - The pot. */
  const potValue = (p: Pot) => (edits[p.key] !== undefined ? Number(edits[p.key]) : potOriginal(p));

  /** The saved value of a field. @param key - Wire key. @returns Name string, name list, or text. */
  const fieldOriginal = (key: string): any => {
    if (key === 'managers') return record.people.managers || [];
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

  // Header values follow unsaved edits live.
  const currentName = String(fieldValue('name'));
  const initials = currentName.split(' ').map((w) => w[0] || '').join('').slice(0, 2).toUpperCase();
  const currentEmail = String(fieldValue('email'));
  const currentDept = String(fieldValue('department'));
  const currentLoc = String(fieldValue('location'));
  const currentMgrs = fieldValue('managers') as string[];

  // The person's requests, filtered by the segmented control.
  const shownRequests = requests.filter((r) => {
    if (reqFilter === 'all') return true;
    if (reqFilter === 'pending') return (r.Status || 'Pending') === 'Pending';
    if (reqFilter === 'mu') return r.request_type === 'overtime';
    return r.request_type === 'leave';                           // "leave"
  });

  return (
    <>
      <Box sx={{ bgcolor: 'background.paper', border: 1, borderColor: 'divider', borderRadius: 3, overflow: 'hidden' }}>
        {/* Header: back, avatar, name (editable), email, placement, badges. */}
        <Box sx={{ display: 'flex', gap: 2, alignItems: 'center', p: 2.25, borderBottom: 1, borderColor: 'divider', flexWrap: 'wrap' }}>
          <IconButton aria-label="Back to staff" onClick={onBack} sx={{ border: 1, borderColor: t.line2, borderRadius: 2 }}>
            <ArrowBackIcon />
          </IconButton>
          <Box sx={{ width: 52, height: 52, borderRadius: 2, bgcolor: 'primary.main', color: 'primary.contrastText', display: 'grid', placeItems: 'center', fontFamily: t.display, fontSize: 22 }}>
            {initials}
          </Box>
          <Box sx={{ flex: 1, minWidth: 0 }}>
            {editingKey === 'name' ? (
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
          <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap" justifyContent="flex-end">
            {fieldValue('salary_hourly') && <Chip size="small" label={String(fieldValue('salary_hourly'))} />}
            {!currentMgrs.length && <Chip size="small" color="error" variant="outlined" label="No manager" />}
            {fieldValue('system_check') === 'Editing' && <Chip size="small" color="warning" variant="outlined" label="System check: Editing" />}
            {setupProblems.map((p) => <Chip key={p} size="small" color="error" variant="outlined" label={p} />)}
          </Stack>
        </Box>

        {/* Body: balances + history on the left, requests + details on the right. */}
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'minmax(0, 1.25fr) minmax(0, 1fr)' } }}>
          <Box sx={{ p: 2.5, display: 'grid', gap: 2.5, alignContent: 'start', minWidth: 0 }}>
            <SectionHead title="Entitlements and balances" hint="Click a tile to change it" />
            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 1.25 }}>
              {POTS.map((p) => (
                <Tile
                  key={p.key} pot={p} value={potValue(p)} saved={potOriginal(p)}
                  entitlement={p.of ? potValue(POT_BY_KEY[p.of]) : undefined}
                  onChange={(v) => setEdit(p.key, v)}
                />
              ))}
            </Box>

            <SectionHead title="History" hint="Every change, with its reason" />
            <Box sx={{ display: 'grid', gap: 1.5 }}>
              {record.history.length === 0 ? (
                <Typography color="text.secondary">No changes made from the app yet.</Typography>
              ) : record.history.map((h, i) => (
                <Box key={i} sx={{ display: 'grid', gridTemplateColumns: '18px minmax(0, 1fr)', gap: 1.5 }}>
                  <Box sx={{ width: 14, height: 14, mt: 0.5, borderRadius: '50%', border: `3px solid ${theme.palette.primary.main}`, bgcolor: 'background.paper' }} />
                  <Box>
                    <Typography sx={{ fontSize: 14 }}>
                      {HISTORY_LABELS[h.field] || h.field}{' '}
                      <b style={{ fontVariantNumeric: 'tabular-nums' }}>{String(h.old_value ?? '')} → {String(h.new_value ?? '')}</b>
                    </Typography>
                    <Typography variant="caption" color="text.secondary">{niceDateTime(h.changed_at)} · {h.reason}</Typography>
                  </Box>
                </Box>
              ))}
            </Box>
          </Box>

          <Box sx={{ p: 2.5, display: 'grid', gap: 2.5, alignContent: 'start', minWidth: 0, borderTop: { xs: 1, md: 0 }, borderLeft: { md: 1 }, borderColor: { xs: 'divider', md: 'divider' } }}>
            <SectionHead title="Requests" />
            <ToggleButtonGroup
              exclusive size="small" value={reqFilter}
              onChange={(_, v) => v && setReqFilter(v)} aria-label="Filter requests"
            >
              <ToggleButton value="all">All</ToggleButton>
              <ToggleButton value="leave">Leave</ToggleButton>
              <ToggleButton value="mu">Make-up</ToggleButton>
              <ToggleButton value="pending">Pending</ToggleButton>
            </ToggleButtonGroup>
            <Enter key={reqFilter}>
              {shownRequests.length === 0 ? (
                <Typography color="text.secondary">Nothing here.</Typography>
              ) : (
                <Stack divider={<Box sx={{ borderTop: 1, borderColor: 'divider' }} />}>
                  {shownRequests.map((r) => (
                    <Box key={`${r.request_type}-${r.id}`} sx={{ display: 'flex', gap: 1, alignItems: 'center', py: 1 }}>
                      <KindPill kind={requestKind(r)} label={requestWhat(r)} />
                      <Typography variant="body2" sx={{ flex: 1, minWidth: 0 }}>{requestWhen(r)}</Typography>
                      <Chip size="small" color={STATUS_COLOR[r.Status] || 'default'} label={r.Status || 'Pending'} />
                    </Box>
                  ))}
                </Stack>
              )}
            </Enter>

            <SectionHead title="Details" hint="Click any field to change it" />
            <FieldGroup title="Contact">
              {['email', 'cell', 'extension', 'birthday'].map((k) => renderField(k))}
            </FieldGroup>
            <FieldGroup title="Placement">
              {['location', 'department', 'salary_hourly', 'supervisor_text', 'system_check', 'request_allow_date', 'comments'].map((k) => renderField(k))}
            </FieldGroup>
            <Box>
              <GroupTitle>Managers</GroupTitle>
              <ManagersEditor
                names={currentMgrs} savedNames={fieldOriginal('managers') as string[]}
                options={peopleNames.filter((n) => !currentMgrs.includes(n) && n !== currentName)}
                onChange={(names) => setEdit('managers', names)}
              />
            </Box>
            <FieldGroup title="Microsoft 365">
              {['title_link', 'supervisor_link'].map((k) => renderField(k))}
            </FieldGroup>
            <FieldGroup title="Record">
              <Fact label="Staff Directory ID" value={record.employee_id} system />
              <Fact label="Last modified" value={String(record.fields.Modified ?? '')} system />
            </FieldGroup>
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
    </>
  );

  /** Render one detail field by its wire key. @param key - The field. */
  function renderField(key: string) {
    const f = FIELDS[key];
    const open = editingKey === key;
    const value = fieldValue(key);
    const dirty = edits[key] !== undefined;
    const display = f.type === 'person' ? ((value as PersonEdit)?.name || (typeof value === 'string' ? value : '')) : String(value);

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
            field={f} value={value} people={peopleNames}
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
 * @param props.onCommit - Called with the new value.
 * @param props.onCancel - Called to drop the edit.
 * @returns The editor control.
 */
function FieldEditor({ field, value, people, onCommit, onCancel }: {
  field: FieldDef; value: any; people: string[]; onCommit: (v: any) => void; onCancel: () => void;
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
    return (
      <Autocomplete
        openOnFocus size="small" sx={{ mt: 0.5 }} options={people} value={current || null}
        onChange={(_, picked) => onCommit({ sp_user_id: null, name: picked || '' })}
        renderInput={(params) => <TextField {...params} autoFocus placeholder="Search Microsoft 365 people" aria-label={field.label} onKeyDown={(e) => { if (e.key === 'Escape') onCancel(); }} />}
      />
    );
  }
  // text, email, tel, date, area
  const isArea = field.type === 'area';
  return (
    <TextField
      autoFocus fullWidth size="small" multiline={isArea} minRows={isArea ? 3 : undefined}
      type={field.type === 'area' ? undefined : field.type} defaultValue={String(value)} sx={{ mt: 0.5 }}
      inputProps={{ 'aria-label': field.label }}
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
function Tile({ pot, value, saved, entitlement, onChange }: {
  pot: Pot; value: number; saved: number; entitlement?: number; onChange: (v: number) => void;
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
      tabIndex={editing ? -1 : 0} role="button" aria-label={`Change ${pot.label}`}
      onClick={() => !editing && setEditing(true)}
      onKeyDown={(e) => { if (!editing && e.key === 'Enter') setEditing(true); }}
      sx={{
        position: 'relative', border: 1, borderRadius: 2, p: 1.5, display: 'grid', gap: 0.5, textAlign: 'left',
        cursor: editing ? 'default' : 'pointer',
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
            autoFocus inputRef={inputRef} size="small" type="number" defaultValue={fmt(value)} inputProps={{ step: pot.step, 'aria-label': `New ${pot.label}`, style: { textAlign: 'center', fontVariantNumeric: 'tabular-nums' } }}
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
