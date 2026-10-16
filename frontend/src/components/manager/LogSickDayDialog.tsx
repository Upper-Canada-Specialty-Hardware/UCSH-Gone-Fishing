import { useMemo, useState } from 'react';
import {
  Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  MenuItem, Stack, TextField, Typography,
} from '@mui/material';
import { addDays, dayKey, isWeekend, parseDay, today } from '../../utils/dates';
import { isHourly } from '../../utils/employment';

/** A team member as returned by GET /team/members (only what this dialog reads). */
interface Member {
  id: string;                                                  // SharePoint item id
  name: string;                                                // display name for the picker
  balances?: { sick_balance?: number; hourly?: boolean };   // sick left + the hourly flag
}

/** The body POST /team/sick-days accepts (snake_case, as the backend expects). */
export interface SickDayPayload {
  employee_id: string;    // whose sick day it is
  start_date: string;     // "YYYY-MM-DD", first day off
  end_date?: string;      // "YYYY-MM-DD"; omitted means a single day
  notes?: string;         // optional, shown on the request description
}

/** What the backend returns once the sick day is approved. */
export interface SickDayResult {
  status: string;                       // "approved"
  request_id: string;                   // the created request's id
  employee_name: string;                // whose day was logged
  days: number;                         // working days deducted
  balances?: Record<string, unknown>;   // their refreshed balances
}

interface Props {
  /** Whether the dialog is showing. */
  open: boolean;
  /** The manager's team, for the employee picker. */
  members: Member[];
  /** Close without submitting. */
  onClose: () => void;
  /** The API call that records and approves the sick day. */
  submitSickDay: (payload: SickDayPayload) => Promise<{ data: SickDayResult }>;
  /** Notify the parent after success so it can toast and refresh the team data. */
  onLogged: (result: SickDayResult) => void;
}

/**
 * A day count trimmed like the backend's `:g` format (no trailing zeros).
 *
 * @param n - The number of days (may be negative).
 * @returns e.g. "-1.5", "-2", "3".
 */
function trimDays(n: number): string {
  return String(Math.round(n * 1000) / 1000);   // 3 decimals, trailing zeros dropped
}

/**
 * A rough count of weekdays in an inclusive date range, used only to preview how
 * far a sick day would push the sick balance. Weekends are skipped; company
 * holidays are not (the backend does the exact sum), so the preview is labelled
 * "about".
 *
 * @param start - First day, "YYYY-MM-DD".
 * @param end - Last day, "YYYY-MM-DD"; a missing end means a single day.
 * @returns The weekday count, or 0 when the range is empty or back to front.
 */
function roughWeekdays(start: string, end: string): number {
  const a = parseDay(start);           // first day at local noon
  if (!a) return 0;                    // no start, nothing to count
  const b = parseDay(end) || a;        // missing end means one day
  if (b < a) return 0;                 // end before start is not a real range
  let n = 0;                           // running weekday tally
  for (let d = a; d <= b; d = addDays(d, 1)) {   // step one day at a time
    if (!isWeekend(d)) n += 1;         // count Mon to Fri only
  }
  return n;
}

/**
 * Dialog for a manager to log a sick day on an employee's behalf. The backend
 * creates the "Sick or Personal Day" request in the employee's name and approves
 * it at once, so the dialog says so plainly before the manager confirms. For a BC
 * hourly person it also shows their sick balance and warns, without blocking, when
 * logging would take Sick below zero. Only mounted when the backend reports the
 * manager's location is enabled (`can_log_sick_days` on GET /team/members).
 *
 * @param props - See {@link Props}.
 * @returns The dialog.
 */
export default function LogSickDayDialog({ open, members, onClose, submitSickDay, onLogged }: Props) {
  const [employeeId, setEmployeeId] = useState('');          // picked team member
  const [startDate, setStartDate] = useState(() => dayKey(today()));   // defaults to today
  const [endDate, setEndDate] = useState('');                // blank means a single day
  const [notes, setNotes] = useState('');                    // optional description
  const [saving, setSaving] = useState(false);               // request in flight
  const [error, setError] = useState('');                    // backend or validation reason

  const selected = members.find((m) => String(m.id) === employeeId);   // the chosen member
  const bc = isHourly(selected?.balances);                 // hourly gets the sick-balance note
  const sickBalance = selected?.balances?.sick_balance ?? 0; // their sick left, days

  // A rough preview of how far this logs their Sick: weekdays in the range,
  // subtracted from what they have now. Recomputed as the dates change.
  const wholeDays = useMemo(() => roughWeekdays(startDate, endDate), [startDate, endDate]);
  const sickAfter = sickBalance - wholeDays;                 // their sick left afterwards
  const belowZero = bc && wholeDays > 0 && sickAfter < 0;    // warn, never block, for hourly

  /** Reset the form so the next open starts clean. */
  const reset = () => {
    setEmployeeId('');
    setStartDate(dayKey(today()));
    setEndDate('');
    setNotes('');
    setError('');
  };

  /** Moving the start past a set end drags the end along; a single day is the common case. */
  const changeStart = (value: string) => {
    setStartDate(value);                                     // take the new start
    if (endDate && value > endDate) setEndDate(value);       // keep end on or after it
  };

  /** Validate locally, then submit and surface the backend's reason on failure. */
  const submit = async () => {
    setError('');
    if (!employeeId) { setError('Choose the employee the sick day is for.'); return; }   // picker is required
    if (!startDate) { setError('A start date is required.'); return; }                   // start is required
    if (endDate && endDate < startDate) { setError('The end date must be on or after the start date.'); return; }   // sane range
    setSaving(true);
    try {
      // end_date and notes are optional: send them only when the manager set them.
      const res = await submitSickDay({
        employee_id: employeeId,
        start_date: startDate,
        ...(endDate ? { end_date: endDate } : {}),           // omitted means a single day
        ...(notes.trim() ? { notes: notes.trim() } : {}),    // omitted means no note
      });
      onLogged(res.data);                                    // parent toasts and refreshes
      reset();                                               // clear for next time
      onClose();                                             // dismiss
    } catch (err: any) {
      const detail = err?.response?.data?.detail;            // the backend's readable reason
      setError(typeof detail === 'string' ? detail : 'The sick day could not be logged. Please try again.');
    } finally {
      setSaving(false);                                      // re-enable the buttons
    }
  };

  return (
    <Dialog open={open} onClose={saving ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Log a sick day</DialogTitle>
      <DialogContent>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            select label="Employee" required value={employeeId}
            onChange={(e) => setEmployeeId(e.target.value)} fullWidth
          >
            {members.map((m) => (
              <MenuItem key={m.id} value={String(m.id)}>{m.name}</MenuItem>
            ))}
          </TextField>
          <TextField
            label="Start date" type="date" required value={startDate}
            onChange={(e) => changeStart(e.target.value)} fullWidth
            slotProps={{ inputLabel: { shrink: true } }}
          />
          <TextField
            label="End date (optional)" type="date" value={endDate}
            onChange={(e) => setEndDate(e.target.value)} fullWidth
            slotProps={{ inputLabel: { shrink: true } }}
            helperText="Leave blank for a single day. Weekends and company holidays are not counted."
          />
          <TextField
            label="Notes (optional)" value={notes}
            onChange={(e) => setNotes(e.target.value)} fullWidth multiline rows={2}
            helperText="Shown on the request as a note from you."
          />
          {/* hourly: show their sick left and that it is whole days, approved on the spot. */}
          {selected && bc && (
            <Stack spacing={0.5}>
              <Typography variant="body2">
                {selected.name} has {trimDays(sickBalance)} sick day{sickBalance === 1 ? '' : 's'} left.
              </Typography>
              <Typography variant="body2" color="text.secondary">
                Sick days are taken in full days. This is approved straight away.
              </Typography>
              {/* Below-zero preview, worded like the shared belowZeroSentence; never blocks submit. */}
              {belowZero && (
                <Typography variant="body2" color="error.main" sx={{ fontWeight: 600 }}>
                  Logging about {wholeDays} day{wholeDays === 1 ? '' : 's'} takes Sick below zero, to {trimDays(sickAfter)} days.
                  {' '}You can still log it; that is your call.
                </Typography>
              )}
            </Stack>
          )}
          {/* Anyone else: a plain line that it lands approved in the employee's name. */}
          {selected && !bc && (
            <Typography variant="body2" color="text.secondary">
              This is logged in {selected.name}'s name and approved straight away.
            </Typography>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={saving}>Cancel</Button>
        <Button onClick={submit} variant="contained" disabled={saving}>
          {saving ? 'Logging…' : 'Log and approve'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
