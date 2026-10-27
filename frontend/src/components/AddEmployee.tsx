import { useState } from 'react';
import {
  Paper, Typography, TextField, MenuItem, Button, Alert, Stack,
  Divider, CircularProgress, Box, Select, InputLabel, FormControl,
  OutlinedInput, Chip,
} from '@mui/material';
import { isHourly } from '../utils/employment';
import type { EmployeeCheckResult } from '../api/client';

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

/**
 * Employment type drives real balance logic on the backend: "Salary" keeps all
 * five pots; "Hourly" (every hourly employee, wherever they work, #300) has only
 * Sick, with vacation tracked as a counter, so it is created with a vacation
 * entitlement of 0. The older "Hourly BC" value still works on existing records
 * but is not offered for new people.
 */
const SALARY_HOURLY = ['Salary', 'Hourly'];

/** One selectable supervisor, as returned by /admin/sp-users. */
export interface ManagerOption {
  sp_user_id: number;
  name: string;
}

interface Props {
  /** Whether writes are enabled; the form is disabled in reporting-only mode. */
  processingEnabled: boolean;
  /** The API call that creates the employee, e.g. createEmployee or createEmployeeAdmin. */
  submitEmployee: (payload: Record<string, unknown>) => Promise<unknown>;
  /**
   * The API call that checks the typed details without writing, e.g.
   * checkEmployee or checkEmployeeAdmin. Review runs this and shows what will be
   * saved before the person commits.
   */
  checkEmployee: (payload: Record<string, unknown>) => Promise<{ data: EmployeeCheckResult }>;
  /** Notify the parent so it can toast success and refresh. */
  onCreated: (name: string) => void;
  /**
   * When provided, the form shows a supervisor picker and requires at least one
   * choice, sending the picks as `manager_ids` (admin dashboard). When omitted,
   * the caller assigns the supervisor itself and no picker is shown (manager
   * dashboard, which assigns the signed-in manager).
   */
  managerOptions?: ManagerOption[];
}

/** What the server reports back after a create, beyond the record itself. */
interface CreateOutcome {
  name: string;
  notices: string[];
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
 * Form to add a new employee — the guided version of typing a new row into the
 * Staff Directory list. It collects exactly the columns the backend needs and
 * surfaces the backend's validation message inline. Used on both dashboards:
 * the manager version auto-assigns the signed-in manager as supervisor and
 * omits `managerOptions`; the admin version passes the staff list so an admin
 * can choose.
 *
 * @param props - See {@link Props}.
 * @returns The Add Employee panel.
 */
export default function AddEmployee({
  processingEnabled, submitEmployee, checkEmployee, onCreated, managerOptions,
}: Props) {
  const [form, setForm] = useState({ ...EMPTY });
  const [outcome, setOutcome] = useState<CreateOutcome | null>(null);
  const [managerIds, setManagerIds] = useState<number[]>([]);
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  // The review result from the check endpoint; non-null means the confirm panel
  // is showing instead of the form.
  const [review, setReview] = useState<EmployeeCheckResult | null>(null);
  const [error, setError] = useState('');

  const showPicker = Array.isArray(managerOptions);

  /** Update one field by name. @param key - form key. @param value - new value. */
  const set = (key: string, value: string) => setForm((f) => ({ ...f, [key]: value }));

  // Whether the chosen type is hourly, which has no vacation entitlement (#300).
  const bc = isHourly(form.salary_hourly);

  /**
   * Change the employment type. Picking "Hourly" seeds the entitlements hourly
   * staff get by default: no vacation entitlement, five sick days (#300).
   *
   * @param value - The chosen SalaryHourly value.
   */
  const setType = (value: string) =>
    setForm((f) => (isHourly(value)
      ? { ...f, salary_hourly: value, vacation_entitlement: '0', sick_entitlement: f.sick_entitlement || '5' }
      : { ...f, salary_hourly: value }));

  // Only the admin path carries manager_ids; the manager path lets the server
  // assign the supervisor from the signed-in identity.
  const payloadOf = () => (showPicker ? { ...form, manager_ids: managerIds } : { ...form });

  // Whether the review turned up anything the create would refuse.
  const hasBlock = (review?.problems ?? []).some((p) => p.level === 'block');

  /** Check the typed details (no write) and show the confirm panel. */
  const startReview = async () => {
    setError('');
    setOutcome(null);
    setChecking(true);
    try {
      const res = await checkEmployee(payloadOf());
      setReview(res?.data ?? { cleaned: {}, changes: [], problems: [] });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'The details could not be checked. Please try again.');
    } finally {
      setChecking(false);
    }
  };

  /** Submit the form, showing the backend's reason on a 400. */
  const submit = async () => {
    setError('');
    setOutcome(null);
    setSaving(true);
    try {
      const res: any = await submitEmployee(payloadOf());
      const data = res?.data ?? {};
      // Notices (a welcome email that failed) are reported, never blocking.
      setOutcome({
        name: form.title.trim(),
        notices: data.notices ?? [],
      });
      onCreated(form.title.trim());
      setForm({ ...EMPTY });
      setManagerIds([]);
      setReview(null);               // back to a blank form once it is saved
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'The employee could not be created. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  // The fields the backend refuses to create a record without. The button stays
  // disabled until they are present; the rest of the rules are enforced on the
  // server and echoed on error. A supervisor is required only when the admin
  // picker is shown — the manager path supplies one itself.
  const ready =
    !!form.title.trim() &&
    !!form.email_address.trim() &&
    !!form.location &&
    !!form.department.trim() &&
    !!form.salary_hourly &&
    // hourly may have 0 vacation entitlement (never negative); everyone else needs > 0.
    (bc ? Number(form.vacation_entitlement) >= 0 : Number(form.vacation_entitlement) > 0) &&
    Number(form.sick_entitlement) > 0 &&
    (!showPicker || managerIds.length > 0);

  const nameFor = (id: number) => managerOptions?.find((m) => m.sp_user_id === id)?.name ?? String(id);

  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h6" sx={{ fontWeight: 600, mb: 0.5 }}>Add Employee</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Creates a new staff record set up so their leave and overtime requests work.
        {showPicker
          ? ' Choose their supervisor(s) below.'
          : ' The new hire is added to your team.'}{' '}
        They can use the request page right away; any requests they already made there
        are sent once they are added.
      </Typography>

      {!processingEnabled && (
        <Alert severity="info" sx={{ mb: 2 }}>
          System is in reporting-only mode. Adding an employee is disabled.
        </Alert>
      )}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {outcome && <CreatedSummary outcome={outcome} />}

      {review && (
        <ConfirmPanel
          review={review}
          hasBlock={hasBlock}
          saving={saving}
          processingEnabled={processingEnabled}
          supervisorNames={showPicker ? managerIds.map(nameFor) : null}
          onBack={() => setReview(null)}
          onConfirm={submit}
        />
      )}

      {!review && (
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
          value={form.salary_hourly} onChange={(e) => setType(e.target.value)}
          helperText={bc ? 'hourly staff have only a sick balance; vacation is counted, not a balance.' : undefined}
        >
          {SALARY_HOURLY.map((s) => <MenuItem key={s} value={s}>{s}</MenuItem>)}
        </TextField>

        {showPicker && (
          <FormControl required>
            <InputLabel id="supervisor-label">Supervisor(s)</InputLabel>
            <Select
              labelId="supervisor-label"
              multiple
              value={managerIds}
              onChange={(e) => setManagerIds(
                (typeof e.target.value === 'string'
                  ? e.target.value.split(',').map(Number)
                  : e.target.value) as number[],
              )}
              input={<OutlinedInput label="Supervisor(s)" />}
              renderValue={(selected) => (
                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                  {(selected as number[]).map((id) => <Chip key={id} label={nameFor(id)} size="small" />)}
                </Box>
              )}
            >
              {(managerOptions ?? []).map((m) => (
                <MenuItem key={m.sp_user_id} value={m.sp_user_id}>{m.name}</MenuItem>
              ))}
            </Select>
          </FormControl>
        )}

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
            inputProps={{ min: 0 }}
            helperText={bc ? 'hourly staff have no vacation entitlement.' : undefined}
          />
          <TextField
            label="Sick days / year" required type="number" fullWidth
            value={form.sick_entitlement} onChange={(e) => set('sick_entitlement', e.target.value)}
          />
        </Box>

        <Divider textAlign="left">
          <Typography variant="caption" color="text.secondary">
            Opening balances
          </Typography>
        </Divider>
        <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
          {/* hourly staff have only a Sick balance; the other pots do not apply to them (#300). */}
          {!bc && (
            <TextField
              label="Vacation" type="number" sx={{ flex: '1 1 150px' }}
              value={form.vacation_balance} onChange={(e) => set('vacation_balance', e.target.value)}
            />
          )}
          <TextField
            label="Sick" type="number" sx={{ flex: '1 1 150px' }}
            value={form.sick_balance} onChange={(e) => set('sick_balance', e.target.value)}
          />
          {!bc && (
            <>
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
            </>
          )}
        </Box>

        <Box>
          <Button
            variant="contained"
            disabled={!processingEnabled || !ready || checking}
            onClick={startReview}
            startIcon={checking ? <CircularProgress size={16} color="inherit" /> : undefined}
          >
            Review
          </Button>
        </Box>
      </Stack>
      )}
    </Paper>
  );
}

/** A human label for each form field shown in the confirm panel, in order. */
const DETAIL_LABELS: [string, string][] = [
  ['title', 'Full name'],
  ['email_address', 'Email'],
  ['location', 'Location'],
  ['department', 'Department'],
  ['salary_hourly', 'Employment type'],
  ['cell_number', 'Cell number'],
  ['vacation_entitlement', 'Vacation days / year'],
  ['sick_entitlement', 'Sick days / year'],
  ['vacation_balance', 'Opening vacation'],
  ['sick_balance', 'Opening sick'],
  ['overtime_balance', 'Opening make-up'],
  ['carry_over', 'Opening carry-over'],
  ['payout', 'Opening payout'],
];

/**
 * The confirm step: what will be saved, what cleaning changed, and every block
 * or warning the check found. The save button is disabled while any block
 * stands, so the person must go Back and fix it first.
 *
 * @param props.review - The check endpoint result.
 * @param props.hasBlock - Whether any problem is a block.
 * @param props.saving - Whether the create is in flight.
 * @param props.processingEnabled - Writes allowed; the save is disabled otherwise.
 * @param props.supervisorNames - Chosen supervisor names (admin), or null when
 *   the manager path assigns the supervisor itself.
 * @param props.onBack - Return to the form to edit again.
 * @param props.onConfirm - Save the employee.
 * @returns The confirm panel.
 */
function ConfirmPanel({
  review, hasBlock, saving, processingEnabled, supervisorNames, onBack, onConfirm,
}: {
  review: EmployeeCheckResult;
  hasBlock: boolean;
  saving: boolean;
  processingEnabled: boolean;
  supervisorNames: string[] | null;
  onBack: () => void;
  onConfirm: () => void;
}) {
  const cleaned = review.cleaned ?? {};
  // Only the fields that carry a value are worth a row.
  const rows = DETAIL_LABELS.filter(([key]) => {
    const v = cleaned[key];
    return v !== undefined && v !== null && String(v).trim() !== '';
  });
  return (
    <Stack spacing={2} sx={{ maxWidth: 560 }}>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Review before saving</Typography>

      {/* What cleaning changed about the typed details, if anything. */}
      {review.changes.length > 0 && (
        <Alert severity="info">
          <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>Tidied for you</Typography>
          <Stack spacing={0.25}>
            {review.changes.map((c) => (
              <Typography key={c.field} variant="body2" sx={{ wordBreak: 'break-word' }}>
                <Box component="span" sx={{ textDecoration: 'line-through', color: 'text.secondary' }}>
                  {`"${c.before}"`}
                </Box>
                {' → '}
                <Box component="span" sx={{ fontWeight: 600 }}>{`"${c.after}"`}</Box>
              </Typography>
            ))}
          </Stack>
        </Alert>
      )}

      {/* The details as they will be saved. */}
      <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 1, p: 2 }}>
        <Stack spacing={1}>
          {rows.map(([key, label]) => (
            <Box
              key={key}
              sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, justifyContent: 'space-between' }}
            >
              <Typography variant="body2" color="text.secondary">{label}</Typography>
              <Typography variant="body2" sx={{ fontWeight: 600, wordBreak: 'break-word' }}>
                {String(cleaned[key])}
              </Typography>
            </Box>
          ))}
          {supervisorNames && (
            <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, justifyContent: 'space-between' }}>
              <Typography variant="body2" color="text.secondary">Supervisor(s)</Typography>
              <Typography variant="body2" sx={{ fontWeight: 600, wordBreak: 'break-word' }}>
                {supervisorNames.length ? supervisorNames.join(', ') : 'None chosen'}
              </Typography>
            </Box>
          )}
        </Stack>
      </Box>

      {/* Blocks in red, warnings in amber; a block disables the save. */}
      {review.problems.map((p, i) => (
        <Alert key={`${p.code}-${i}`} severity={p.level === 'block' ? 'error' : 'warning'}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>{p.title}</Typography>
          <Typography variant="body2">{p.detail}</Typography>
        </Alert>
      ))}
      {review.problems.length === 0 && (
        <Alert severity="success">Everything checks out. They are ready to add.</Alert>
      )}

      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
        <Button variant="outlined" onClick={onBack} disabled={saving}>Back</Button>
        <Button
          variant="contained"
          onClick={onConfirm}
          disabled={!processingEnabled || hasBlock || saving}
          startIcon={saving ? <CircularProgress size={16} color="inherit" /> : undefined}
        >
          Add employee
        </Button>
      </Box>
    </Stack>
  );
}

/**
 * What happened after a create, with any notices.
 *
 * @param props.outcome - From the create response.
 * @returns A success alert, plus warnings when something needs attention.
 */
function CreatedSummary({ outcome }: { outcome: CreateOutcome }) {
  return (
    <Stack spacing={1} sx={{ mb: 2 }}>
      <Alert severity="success">{outcome.name} was added.</Alert>
      {outcome.notices.map((n) => <Alert key={n} severity="warning">{n}</Alert>)}
    </Stack>
  );
}
