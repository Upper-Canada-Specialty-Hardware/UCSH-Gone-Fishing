import { useState } from 'react';
import {
  Box, Paper, Typography, TextField, MenuItem, Button, Alert, Stack,
  Divider, CircularProgress,
} from '@mui/material';
import { createEmployee } from '../api/client';

/**
 * The Location values the backend maps to a province. Anything outside this set
 * has no province, which blocks leave-day calculation, so the field is a
 * dropdown rather than free text. Kept in step with LOCATION_PROVINCE_MAP in
 * app/services/employee.py.
 */
const LOCATIONS = [
  'Toronto Victoria Park',
  'Toronto Warden',
  'Ottawa',
  'Leaside',
  'Barrie',
  'British Columbia',
  'Newfound Land',
];

/** Employment type drives real balance logic on the backend (`== "Hourly"`). */
const SALARY_HOURLY = ['Salary', 'Hourly'];

interface Props {
  /** Whether writes are enabled; the form is disabled in reporting-only mode. */
  processingEnabled: boolean;
  /** Notify the parent so it can toast success and refresh the team. */
  onCreated: (name: string) => void;
}

/** The blank form. Balances default to zero — a new hire starts empty. */
const EMPTY = {
  title: '',
  email_address: '',
  location: '',
  department: '',
  salary_hourly: '',
  vacation_entitlement: '',
  sick_entitlement: '',
  cell_number: '',
  vacation_balance: '',
  sick_balance: '',
  overtime_balance: '',
  carry_over: '',
  payout: '',
};

/**
 * Form to add a new employee to the team — the guided version of typing a new
 * row into the Staff Directory list. It collects exactly the columns the
 * backend needs, and surfaces the backend's validation message inline rather
 * than after the fact. The new hire is assigned to the signed-in manager as
 * their supervisor, so no manager picker is shown here.
 *
 * @param props - See {@link Props}.
 * @returns The Add Employee tab panel.
 */
export default function AddEmployee({ processingEnabled, onCreated }: Props) {
  const [form, setForm] = useState({ ...EMPTY });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  /** Update one field by name. @param key - form key. @param value - new value. */
  const set = (key: string, value: string) => setForm((f) => ({ ...f, [key]: value }));

  /** Submit the form, showing the backend's reason on a 400. */
  const submit = async () => {
    setError('');
    setSaving(true);
    try {
      await createEmployee(form);
      onCreated(form.title.trim());
      setForm({ ...EMPTY });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'The employee could not be created. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  // The four fields the backend refuses to create a record without. The button
  // stays disabled until they are present, so the obvious mistakes never reach
  // the server; the rest of the rules are enforced there and echoed on error.
  const ready =
    !!form.title.trim() &&
    !!form.email_address.trim() &&
    !!form.location &&
    !!form.department.trim() &&
    !!form.salary_hourly &&
    Number(form.vacation_entitlement) > 0 &&
    Number(form.sick_entitlement) > 0;

  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h6" sx={{ fontWeight: 600, mb: 0.5 }}>Add Employee</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Creates a new staff record set up so their leave and overtime requests work. The new
        hire is added to your team. Their Microsoft 365 account must already exist.
      </Typography>

      {!processingEnabled && (
        <Alert severity="info" sx={{ mb: 2 }}>
          System is in reporting-only mode. Adding an employee is disabled.
        </Alert>
      )}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      <Stack spacing={2} sx={{ maxWidth: 560 }}>
        <TextField
          label="Full name (as in Microsoft 365)" required
          value={form.title} onChange={(e) => set('title', e.target.value)}
          helperText="Must match their Microsoft 365 display name exactly."
        />
        <TextField
          label="Email address" required type="email"
          value={form.email_address} onChange={(e) => set('email_address', e.target.value)}
        />
        <TextField
          label="Location" required select
          value={form.location} onChange={(e) => set('location', e.target.value)}
        >
          {LOCATIONS.map((l) => <MenuItem key={l} value={l}>{l}</MenuItem>)}
        </TextField>
        <TextField
          label="Department" required
          value={form.department} onChange={(e) => set('department', e.target.value)}
        />
        <TextField
          label="Employment type" required select
          value={form.salary_hourly} onChange={(e) => set('salary_hourly', e.target.value)}
        >
          {SALARY_HOURLY.map((s) => <MenuItem key={s} value={s}>{s}</MenuItem>)}
        </TextField>
        <TextField
          label="Cell number (optional)"
          value={form.cell_number} onChange={(e) => set('cell_number', e.target.value)}
          helperText="Only needed if they will approve requests by text."
        />

        <Divider textAlign="left">
          <Typography variant="caption" color="text.secondary">Yearly entitlements</Typography>
        </Divider>
        <Box sx={{ display: 'flex', gap: 2 }}>
          <TextField
            label="Vacation days / year" required type="number" fullWidth
            value={form.vacation_entitlement} onChange={(e) => set('vacation_entitlement', e.target.value)}
          />
          <TextField
            label="Sick days / year" required type="number" fullWidth
            value={form.sick_entitlement} onChange={(e) => set('sick_entitlement', e.target.value)}
          />
        </Box>

        <Divider textAlign="left">
          <Typography variant="caption" color="text.secondary">
            Opening balances (leave blank for a new hire)
          </Typography>
        </Divider>
        <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
          <TextField
            label="Vacation" type="number" sx={{ flex: '1 1 150px' }}
            value={form.vacation_balance} onChange={(e) => set('vacation_balance', e.target.value)}
          />
          <TextField
            label="Sick" type="number" sx={{ flex: '1 1 150px' }}
            value={form.sick_balance} onChange={(e) => set('sick_balance', e.target.value)}
          />
          <TextField
            label="Make-up" type="number" sx={{ flex: '1 1 150px' }}
            value={form.overtime_balance} onChange={(e) => set('overtime_balance', e.target.value)}
          />
          <TextField
            label="Carry-over" type="number" sx={{ flex: '1 1 150px' }}
            value={form.carry_over} onChange={(e) => set('carry_over', e.target.value)}
          />
          <TextField
            label="Payout" type="number" sx={{ flex: '1 1 150px' }}
            value={form.payout} onChange={(e) => set('payout', e.target.value)}
          />
        </Box>

        <Box>
          <Button
            variant="contained"
            disabled={!processingEnabled || !ready || saving}
            onClick={submit}
            startIcon={saving ? <CircularProgress size={16} color="inherit" /> : undefined}
          >
            Add Employee
          </Button>
        </Box>
      </Stack>
    </Paper>
  );
}
