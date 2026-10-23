import { useState } from 'react';
import {
  Alert, Box, Button, CircularProgress, MenuItem, Stack, TextField,
  ToggleButton, ToggleButtonGroup, Typography,
} from '@mui/material';
import {
  updateExistingFromHeld,
  type HeldExisting, type HeldForAdd, type HeldProfileResult, type SetupScope,
} from '../api/client';

interface Props {
  /** The held request, still waiting on the viewer, with the record that has its name. */
  held: HeldForAdd & { existing: HeldExisting };
  /** 'team' for the supervisor, 'admin' from the Held Requests list. */
  scope: SetupScope;
  /** Whether writes are enabled; the save is disabled in reporting-only mode. */
  processingEnabled: boolean;
  /** The offices a record may be at (the Add Employee list). */
  locations: string[];
  /** The employment types offered (the Add Employee list). */
  payTypes: string[];
}

/** The form's values, all as typed text until saved. */
interface EditForm {
  email: string;
  department: string;
  salary_hourly: string;
  location: string;
  vacation_entitlement: string;
  sick_entitlement: string;
  vacation: string;
  sick: string;
  reason: string;
}

/**
 * Add employee on a name already on staff: edit that record instead (#444).
 *
 * Opens on what is on record now, each field showing it underneath, so the
 * supervisor sees the record before anything changes. They decide every change
 * (email, department, pay, office, manager, entitlements, balances) and give a
 * reason. On save, the old email address and any manager taken off are told; the
 * held request goes through once the record carries the address it came from.
 *
 * @param props - See {@link Props}.
 * @returns The profile edit panel.
 */
export default function HeldProfileEdit({ held, scope, processingEnabled, locations, payTypes }: Props) {
  const record = held.existing;
  // Start from the record, never from the request: nothing changes unless chosen.
  const [form, setForm] = useState<EditForm>({
    email: record.email,
    department: record.department,
    salary_hourly: record.salary_hourly,
    location: record.location,
    vacation_entitlement: String(record.vacation_entitlement),
    sick_entitlement: String(record.sick_entitlement),
    vacation: String(record.vacation),
    sick: String(record.sick),
    reason: '',
  });
  const [mode, setMode] = useState<'replace' | 'alongside'>('replace');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<HeldProfileResult | null>(null);

  /** Update one field. @param key - form key. @param value - new value. */
  const set = (key: keyof EditForm, value: string) => setForm((f) => ({ ...f, [key]: value }));

  // "Me" for a manager; the supervisor holding it for an admin acting for them.
  const who = scope === 'team' ? 'me' : held.supervisor_name;
  const hasManagers = record.managers.length > 0;
  // An existing type outside the offered list (e.g. "Hourly BC") stays selectable.
  const pays = payTypes.includes(record.salary_hourly) || !record.salary_hourly
    ? payTypes : [...payTypes, record.salary_hourly];
  const places = locations.includes(record.location) || !record.location
    ? locations : [...locations, record.location];

  /** Save the edit, sending only what the form shows, and report what happened. */
  const save = async () => {
    setError('');
    setSaving(true);
    try {
      const res = await updateExistingFromHeld(held.held_id, scope, {
        changes: {
          vacation_entitlement: Number(form.vacation_entitlement),
          sick_entitlement: Number(form.sick_entitlement),
          vacation: Number(form.vacation),
          sick: Number(form.sick),
        },
        fields: {
          email: form.email.trim(),
          department: form.department.trim(),
          salary_hourly: form.salary_hourly,
          location: form.location,
        },
        manager_mode: mode,
        reason: form.reason.trim(),
      });
      setResult(res.data);                                // the outcome replaces the form
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'The record could not be saved. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  if (result) {
    const sent = result.released.find((r) => r.status === 'released');
    return (
      <Stack spacing={1}>
        <Alert severity="success">{record.name}&apos;s record was saved.</Alert>
        {sent && <Alert severity="success">Their request is now request #{sent.detail}.</Alert>}
        {result.held_status !== 'released' && result.note && <Alert severity="warning">{result.note}</Alert>}
      </Stack>
    );
  }

  /** "On record: X" under a field. @param value - The stored value. */
  const onRecord = (value: string | number) => `On record: ${value === '' ? 'blank' : value}`;

  return (
    <Stack spacing={2} sx={{ maxWidth: 560 }}>
      <Alert severity="warning">
        {record.name} is already on staff (#{record.employee_id}). Add employee opened that record instead of
        making a second one. Nothing changes until you save.
      </Alert>
      {error && <Alert severity="error">{error}</Alert>}

      <TextField
        label="Email" value={form.email} onChange={(e) => set('email', e.target.value)}
        helperText={onRecord(record.email)}
      />
      {form.email.trim().toLowerCase() !== held.email.toLowerCase() && (
        <Box>
          {/* The address the request came from; using it lets the request go through. */}
          <Button size="small" onClick={() => set('email', held.email)}>Use {held.email}</Button>
        </Box>
      )}
      <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
        <TextField
          label="Department" sx={{ flex: '1 1 200px' }} value={form.department}
          onChange={(e) => set('department', e.target.value)} helperText={onRecord(record.department)}
        />
        <TextField
          label="Employment type" select sx={{ flex: '1 1 200px' }} value={form.salary_hourly}
          onChange={(e) => set('salary_hourly', e.target.value)} helperText={onRecord(record.salary_hourly)}
        >
          {pays.map((p) => <MenuItem key={p} value={p}>{p}</MenuItem>)}
        </TextField>
      </Box>
      <TextField
        label="Location" select value={form.location}
        onChange={(e) => set('location', e.target.value)} helperText={onRecord(record.location)}
      >
        {places.map((l) => <MenuItem key={l} value={l}>{l}</MenuItem>)}
      </TextField>

      <Box>
        <Typography variant="body2" sx={{ mb: 0.5 }}>Manager</Typography>
        <ToggleButtonGroup
          exclusive size="small" value={mode}
          onChange={(_, v) => v && setMode(v)}
          sx={{ flexWrap: 'wrap' }}
        >
          <ToggleButton value="replace">
            {hasManagers ? `Replace ${record.managers.join(', ')} with ${who}` : `Make ${who} their manager`}
          </ToggleButton>
          {hasManagers && <ToggleButton value="alongside">Add {who} alongside</ToggleButton>}
        </ToggleButtonGroup>
        <Typography variant="caption" color="text.secondary" display="block">
          {onRecord(record.managers.join(', ') || 'none')}
        </Typography>
      </Box>

      <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
        <TextField
          label="Vacation days a year" type="number" sx={{ flex: '1 1 150px' }} value={form.vacation_entitlement}
          onChange={(e) => set('vacation_entitlement', e.target.value)} helperText={onRecord(record.vacation_entitlement)}
        />
        <TextField
          label="Sick days a year" type="number" sx={{ flex: '1 1 150px' }} value={form.sick_entitlement}
          onChange={(e) => set('sick_entitlement', e.target.value)} helperText={onRecord(record.sick_entitlement)}
        />
      </Box>
      <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
        <TextField
          label="Vacation balance" type="number" sx={{ flex: '1 1 150px' }} value={form.vacation}
          onChange={(e) => set('vacation', e.target.value)} helperText={onRecord(record.vacation)}
        />
        <TextField
          label="Sick balance" type="number" sx={{ flex: '1 1 150px' }} value={form.sick}
          onChange={(e) => set('sick', e.target.value)} helperText={onRecord(record.sick)}
        />
      </Box>
      <TextField
        label="Reason" required value={form.reason} onChange={(e) => set('reason', e.target.value)}
        placeholder="Why these changes"
        helperText="Saved with the change history. If the email changes, the old address is told."
      />

      <Box>
        <Button
          variant="contained" onClick={save}
          disabled={!processingEnabled || saving || !form.reason.trim()}
          startIcon={saving ? <CircularProgress size={16} color="inherit" /> : undefined}
        >
          Save profile
        </Button>
      </Box>
    </Stack>
  );
}
