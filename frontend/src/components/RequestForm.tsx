import { Box, ButtonBase, MenuItem, Stack, TextField, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import type { RequestType } from '../api/intake';

/**
 * The leave types the backend accepts, worded exactly as the balance engine
 * matches them (ALLOWED_LEAVE_TYPES in app/services/leave_requests.py).
 */
export const LEAVE_TYPES = [
  'Vacation',
  'Sick or Personal Day',
  'Half Day or Partial Day Off',
  'Bereavement',
  'Jury Duty',
];

export const PARTIAL_DAY = 'Half Day or Partial Day Off';

/** The three forms on the request page, with the label shown on the picker. */
export const REQUEST_TYPES: { value: RequestType; label: string; hint: string }[] = [
  { value: 'leave', label: 'Leave', hint: 'Vacation, sick, part day, bereavement or jury duty' },
  { value: 'overtime', label: 'Overtime', hint: 'Hours worked beyond your schedule' },
  { value: 'carryover-payout', label: 'Carry-over or payout', hint: 'Move vacation days to next year, or cash them out' },
];

/** Every field any of the three forms uses, as typed (strings until sent). */
export interface RequestValues {
  leave_type: string;
  start_date: string;
  end_date: string;
  partial_hours: string;
  notes: string;
  description: string;
  date: string;
  hours: string;
  type_of_request: string;
  days: string;
}

export const EMPTY_REQUEST: RequestValues = {
  leave_type: '', start_date: '', end_date: '', partial_hours: '', notes: '',
  description: '', date: '', hours: '', type_of_request: '', days: '',
};

/**
 * Whether the form has everything the server needs. The server checks again
 * and explains any problem; this only keeps the button disabled until then.
 *
 * @param type - Which form.
 * @param v - The values typed.
 * @returns True when the form can be sent.
 */
export function isRequestReady(type: RequestType, v: RequestValues): boolean {
  if (type === 'leave') {
    if (!v.leave_type || !v.start_date) return false;
    return v.leave_type === PARTIAL_DAY ? Number(v.partial_hours) > 0 : !!v.end_date;
  }
  if (type === 'overtime') return !!v.description.trim() && !!v.date && Number(v.hours) > 0;
  return !!v.type_of_request && Number(v.days) > 0;
}

/**
 * The JSON body for the chosen form, numbers as numbers (snake_case wire format).
 *
 * @param type - Which form.
 * @param v - The values typed.
 * @returns Only the fields that form takes.
 */
export function buildRequestBody(type: RequestType, v: RequestValues): Record<string, unknown> {
  if (type === 'leave') {
    const partial = v.leave_type === PARTIAL_DAY;
    return {
      leave_type: v.leave_type,
      start_date: v.start_date,
      end_date: partial ? null : v.end_date,             // a part day is one date
      partial_hours: partial ? Number(v.partial_hours) : null,
      notes: v.notes.trim() || null,
    };
  }
  if (type === 'overtime') {
    return { description: v.description.trim(), date: v.date, hours: Number(v.hours) };
  }
  return { type_of_request: v.type_of_request, days: Number(v.days) };
}

interface Props {
  /** Which form to show. */
  type: RequestType;
  /** The values typed so far. */
  values: RequestValues;
  /** Update one field. */
  onChange: (key: keyof RequestValues, value: string) => void;
}

/**
 * The leave types as a row of pills; the chosen one fills with the page colour.
 *
 * @param props.value - The chosen type.
 * @param props.onPick - Called with the type picked.
 * @returns The labelled group.
 */
function LeaveChips({ value, onPick }: { value: string; onPick: (t: string) => void }) {
  const theme = useTheme();
  return (
    <Box>
      <Typography variant="body2" sx={{ fontWeight: 600, mb: 1 }} id="leave-type-label">Type of leave</Typography>
      <Box role="group" aria-labelledby="leave-type-label" sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
        {LEAVE_TYPES.map((t) => {
          const on = value === t;
          return (
            <ButtonBase
              key={t}
              aria-pressed={on}
              onClick={() => onPick(t)}
              sx={{
                px: 1.5, py: 0.75, borderRadius: 999, fontSize: 13, fontWeight: on ? 700 : 500,
                border: `1.5px solid ${on ? theme.palette.primary.main : theme.tokens.line2}`,
                bgcolor: on ? 'primary.main' : 'background.paper',
                color: on ? theme.tokens.primaryInk : 'text.primary',
                transition: 'background-color .4s ease, color .4s ease, border-color .4s ease',  // follows the mood change
              }}
            >
              {t}
            </ButtonBase>
          );
        })}
      </Box>
    </Box>
  );
}

/** Two fields side by side on wider screens, stacked on phones. */
const ROW2 = { display: 'grid', gap: 2, gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'repeat(2, minmax(0, 1fr))' } } as const;

/**
 * The fields of one request form: leave, overtime, or carry-over/payout.
 * Plain date inputs, so phones show their own date picker.
 *
 * @param props - See {@link Props}.
 * @returns The form fields, without a submit button.
 */
export default function RequestForm({ type, values: v, onChange }: Props) {
  /** A change handler for one field. @param key - The field. */
  const field = (key: keyof RequestValues) =>
    (e: React.ChangeEvent<HTMLInputElement>) => onChange(key, e.target.value);
  const dateProps = { type: 'date', slotProps: { inputLabel: { shrink: true } } } as const;

  if (type === 'leave') {
    const partial = v.leave_type === PARTIAL_DAY;
    return (
      <Stack spacing={2}>
        <LeaveChips value={v.leave_type} onPick={(t) => onChange('leave_type', t)} />
        <Box sx={ROW2}>
          <TextField label={partial ? 'Date' : 'First day away'} required {...dateProps}
            value={v.start_date} onChange={field('start_date')} />
          {partial ? (
            <TextField label="Hours away" required type="number" value={v.partial_hours}
              onChange={field('partial_hours')}
              slotProps={{ htmlInput: { min: 0.5, max: 7.5, step: 0.5 } }}
              helperText="In half-hour steps, less than a full 8-hour day." />
          ) : (
            <TextField label="Last day away" required {...dateProps}
              value={v.end_date} onChange={field('end_date')} />
          )}
        </Box>
        <TextField label="Notes (optional)" multiline minRows={2} value={v.notes} onChange={field('notes')}
          helperText="Shown to your manager with the request." />
      </Stack>
    );
  }

  if (type === 'overtime') {
    return (
      <Stack spacing={2}>
        <TextField label="What was the overtime for?" required value={v.description} onChange={field('description')} />
        <Box sx={ROW2}>
          <TextField label="Date worked" required {...dateProps} value={v.date} onChange={field('date')} />
          <TextField label="Hours" required type="number" value={v.hours} onChange={field('hours')}
            slotProps={{ htmlInput: { min: 0.5, step: 0.5 } }} helperText="In half-hour steps." />
        </Box>
      </Stack>
    );
  }

  return (
    <Box sx={ROW2}>
      <TextField label="Request" required select value={v.type_of_request} onChange={field('type_of_request')}>
        <MenuItem value="Carry Over">Carry over to next year</MenuItem>
        <MenuItem value="Payout">Pay out</MenuItem>
      </TextField>
      <TextField label="Days" required type="number" value={v.days} onChange={field('days')}
        slotProps={{ htmlInput: { min: 0.5, step: 0.5 } }} helperText="In half-day steps." />
    </Box>
  );
}
