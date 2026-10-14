import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, MenuItem,
  TextField, Typography,
} from '@mui/material';
import { useTheme } from '@mui/material/styles';
import PillToggle from '../PillToggle';
import {
  AdminRequestForm, PreviewResult, StaffOption, createAdminRequest, previewAdminRequest,
} from '../../api/requestsAdmin';
import { Src } from './requestTable';

/** The one leave type measured in hours, not a date range (backend's name). */
const PARTIAL = 'Half Day or Partial Day Off';

/** Src to the request_type the backend expects. */
const TYPE_OF: Record<Src, string> = { leave: 'leave', mu: 'overtime', co: 'carryover-payout' };

/** The balance pots the preview shows, with their keys and labels. */
const POTS: { key: string; label: string }[] = [
  { key: 'vacation_balance', label: 'Vacation' },
  { key: 'sick_balance', label: 'Sick' },
  { key: 'overtime', label: 'Make-Up' },
  { key: 'carryover', label: 'Carry Over' },
  { key: 'payout', label: 'Payout' },
];

interface Props {
  open: boolean;
  onClose: () => void;
  /** The list to start on (the source switch's current list). */
  defaultSrc: Src;
  /** People to pick from, and the leave types, from /admin/requests/new-options. */
  options: { staff: StaffOption[]; leave_types: string[] };
  processingEnabled: boolean;
  /** Told the new request's summary once it is saved; the screen reloads and toasts. */
  onDone: (result: { status: string; item_id: string; request_type: string }) => void;
  onError: (message: string) => void;
}

/** Today's date as YYYY-MM-DD, for the date fields' defaults. */
function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

/**
 * Create a request from the admin dashboard (#283): pick the list, the person
 * (their manager fills in), the list's own fields and whether it is Pending or
 * Approved, with a live panel showing what saving will do to their balances.
 * Pending asks the manager to approve as usual; Approved applies it at once and
 * does not ask the manager.
 *
 * @param props - See {@link Props}.
 * @returns The dialog.
 */
export default function NewRequestDialog({ open, onClose, defaultSrc, options, processingEnabled, onDone, onError }: Props) {
  const theme = useTheme();
  const [src, setSrc] = useState<Src>(defaultSrc === 'co' ? 'co' : defaultSrc);
  const [employeeId, setEmployeeId] = useState('');
  const [status, setStatus] = useState<'Pending' | 'Approved'>('Pending');
  // Leave fields.
  const [leaveType, setLeaveType] = useState('Vacation');
  const [start, setStart] = useState(todayIso());
  const [end, setEnd] = useState(todayIso());
  const [partialHours, setPartialHours] = useState('');
  const [notes, setNotes] = useState('');
  // Make-up fields.
  const [otDate, setOtDate] = useState(todayIso());
  const [otHours, setOtHours] = useState('');
  const [otDesc, setOtDesc] = useState('');
  // Carry-over / payout fields.
  const [coType, setCoType] = useState<'Carry Over' | 'Payout'>('Carry Over');
  const [coDays, setCoDays] = useState('');
  // Preview + saving.
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [saving, setSaving] = useState(false);

  // A fresh open resets to the current list and clears the last entry.
  useEffect(() => {
    if (!open) return;
    setSrc(defaultSrc);
    setEmployeeId(''); setStatus('Pending');
    setLeaveType(options.leave_types[0] || 'Vacation');
    setStart(todayIso()); setEnd(todayIso()); setPartialHours(''); setNotes('');
    setOtDate(todayIso()); setOtHours(''); setOtDesc('');
    setCoType('Carry Over'); setCoDays('');
    setPreview(null);
  }, [open, defaultSrc, options.leave_types]);

  const chosen = options.staff.find((s) => s.id === employeeId) || null;

  /** The form payload for the current list, snake_case as the backend wants. */
  const form = useMemo<AdminRequestForm>(() => {
    if (src === 'leave') {
      return leaveType === PARTIAL
        ? { leave_type: leaveType, start_date: start, partial_hours: Number(partialHours) || 0, notes }
        : { leave_type: leaveType, start_date: start, end_date: end, notes };
    }
    if (src === 'mu') return { description: otDesc, date: otDate, hours: Number(otHours) || 0 };
    return { type_of_request: coType, days: Number(coDays) || 0 };
  }, [src, leaveType, start, end, partialHours, notes, otDate, otHours, otDesc, coType, coDays]);

  /** Whether the form is complete enough to save (mirrors the backend checks). */
  const valid = useMemo(() => {
    if (!employeeId) return false;
    if (src === 'leave') {
      if (!start) return false;
      if (leaveType === PARTIAL) { const h = Number(partialHours); return h > 0 && h < 8 && h % 0.5 === 0; }
      return !!end && end >= start;
    }
    if (src === 'mu') { const h = Number(otHours); return !!otDate && !!otDesc.trim() && h > 0 && h % 0.5 === 0; }
    const d = Number(coDays); return d > 0 && d % 0.5 === 0;
  }, [src, employeeId, start, end, leaveType, partialHours, otDate, otHours, otDesc, coDays]);

  // Ask the server what the request would do to balances, a short wait after
  // the last change, whenever the form is complete. Read-only, so it runs even
  // while processing is off.
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => {
    setPreview(null);
    if (!open || !valid || !employeeId) return;
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      previewAdminRequest({ request_type: TYPE_OF[src], employee_id: employeeId, form })
        .then((r) => setPreview(r.data))
        .catch(() => setPreview(null));                  // a refused preview just shows nothing
    }, 350);
    return () => clearTimeout(timer.current);
  }, [open, valid, employeeId, src, form]);

  /** Save the request, then hand the result back for the screen to reload and toast. */
  const save = async () => {
    setSaving(true);
    try {
      const res = await createAdminRequest({ request_type: TYPE_OF[src], employee_id: employeeId, status, form });
      onDone({ ...res.data, request_type: TYPE_OF[src] });
    } catch (e: any) {
      const detail = e.response?.data?.detail;
      onError(typeof detail === 'string' ? detail : 'The request could not be saved.');
    } finally {
      setSaving(false);
    }
  };

  const listOptions: { key: Src; label: string }[] = [
    { key: 'leave', label: 'Leave' }, { key: 'mu', label: 'Make-up' }, { key: 'co', label: 'Carry-over or payout' },
  ];

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>New request</DialogTitle>
      <DialogContent dividers sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: '1.4fr 1fr' }, gap: 2.5 }}>
        {/* Left: the form. */}
        <Box sx={{ display: 'grid', gap: 2, alignContent: 'start', minWidth: 0 }}>
          <Box>
            <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700 }}>List</Typography>
            <Box sx={{ mt: 0.5 }}>
              <PillToggle name="new-list" label="Which list" value={src} onChange={setSrc} options={listOptions} />
            </Box>
          </Box>

          <TextField
            select required label="Employee" value={employeeId}
            onChange={(e) => setEmployeeId(e.target.value)}
            helperText={chosen ? `Manager: ${chosen.managers || 'none on record'}` : 'From the Staff Directory'}
          >
            <MenuItem value=""><em>Choose a person</em></MenuItem>
            {options.staff.map((s) => (
              <MenuItem key={s.id} value={s.id}>{s.name}{s.department ? ` · ${s.department}` : ''}</MenuItem>
            ))}
          </TextField>

          {src === 'leave' && (
            <>
              <TextField select required label="Leave type" value={leaveType} onChange={(e) => setLeaveType(e.target.value)}>
                {options.leave_types.map((t) => <MenuItem key={t} value={t}>{t === PARTIAL ? 'Part day' : t}</MenuItem>)}
              </TextField>
              {leaveType === PARTIAL ? (
                <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 1.5 }}>
                  <TextField required type="date" label="Date" value={start} onChange={(e) => setStart(e.target.value)} InputLabelProps={{ shrink: true }} />
                  <TextField
                    required type="number" label="Hours away" value={partialHours}
                    onChange={(e) => setPartialHours(e.target.value)}
                    inputProps={{ step: 0.5, min: 0.5, max: 7.5 }}
                    helperText="Less than 8, in half hours"
                  />
                </Box>
              ) : (
                <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 1.5 }}>
                  <TextField required type="date" label="Start date" value={start} onChange={(e) => { setStart(e.target.value); if (end < e.target.value) setEnd(e.target.value); }} InputLabelProps={{ shrink: true }} />
                  <TextField required type="date" label="End date" value={end} onChange={(e) => setEnd(e.target.value)} inputProps={{ min: start }} InputLabelProps={{ shrink: true }} />
                </Box>
              )}
              <TextField label="Title" value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Share some details" />
            </>
          )}

          {src === 'mu' && (
            <>
              <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 1.5 }}>
                <TextField required type="date" label="Date" value={otDate} onChange={(e) => setOtDate(e.target.value)} InputLabelProps={{ shrink: true }} />
                <TextField
                  required type="number" label="Hours" value={otHours} onChange={(e) => setOtHours(e.target.value)}
                  inputProps={{ step: 0.5, min: 0.5 }}
                  helperText={Number(otHours) > 0 ? `= ${(Number(otHours) / 8).toFixed(3)} days of make-up` : 'Half hours allowed'}
                />
              </Box>
              <TextField required label="Overtime details" value={otDesc} onChange={(e) => setOtDesc(e.target.value)} placeholder="e.g. Saturday inventory count" />
            </>
          )}

          {src === 'co' && (
            <>
              <Box>
                <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700 }}>Type of request</Typography>
                <Box sx={{ mt: 0.5 }}>
                  <PillToggle name="co-type" label="Carry over or payout" value={coType} onChange={setCoType} options={[{ key: 'Carry Over', label: 'Carry Over' }, { key: 'Payout', label: 'Payout' }]} />
                </Box>
              </Box>
              <TextField required type="number" label="Days" value={coDays} onChange={(e) => setCoDays(e.target.value)} inputProps={{ step: 0.5, min: 0.5 }} helperText="Moved out of Vacation. Allowed any time (HR's own tool)." />
            </>
          )}

          <Box>
            <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700 }}>Status</Typography>
            <Box sx={{ mt: 0.5 }}>
              <PillToggle name="new-status" label="Status" value={status} onChange={setStatus} options={[{ key: 'Pending', label: 'Pending' }, { key: 'Approved', label: 'Approved' }]} />
            </Box>
          </Box>
        </Box>

        {/* Right: what saving will do, in plain words, with the balance effect. */}
        <Box sx={{ bgcolor: theme.tokens.hover, borderRadius: 2, p: 2, display: 'grid', gap: 1.5, alignContent: 'start' }}>
          <Typography variant="caption" sx={{ fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: 'text.secondary' }}>When you save</Typography>
          {!employeeId ? (
            <Typography variant="body2" color="text.secondary">Choose the employee to see the effect.</Typography>
          ) : (
            <>
              <Typography variant="body2">
                {status === 'Pending'
                  ? `${chosen?.managers || 'The manager'} is emailed to approve it, like any other request.`
                  : `It is approved now, recorded as approved by an admin, and ${chosen?.name || 'the employee'} is emailed. The manager is not asked.`}
              </Typography>
              {preview && (
                <>
                  <Typography variant="body2" color="text.secondary">Counts as {preview.days} day{preview.days === 1 ? '' : 's'}.</Typography>
                  {preview.projected ? (
                    <Box sx={{ bgcolor: 'background.paper', borderRadius: 1.5, p: 1.25, display: 'grid', gap: 0.5 }}>
                      {POTS.filter(({ key }) => (preview.current[key] ?? 0) !== (preview.projected![key] ?? 0)).map(({ key, label }) => {
                        const a = preview.current[key] ?? 0; const b = preview.projected![key] ?? 0;
                        return (
                          <Box key={key} sx={{ display: 'flex', justifyContent: 'space-between', fontVariantNumeric: 'tabular-nums', fontSize: 14 }}>
                            <span>{label}</span>
                            <b style={{ color: b < 0 ? theme.palette.error.main : undefined }}>{a} → {b}</b>
                          </Box>
                        );
                      })}
                      <Typography variant="caption" color="text.secondary">{status === 'Pending' ? 'If approved. Nothing changes until then.' : 'Applied when you save.'}</Typography>
                    </Box>
                  ) : preview.unchanged ? (
                    <Typography variant="body2" sx={{ fontStyle: 'italic' }}>{preview.unchanged}</Typography>
                  ) : null}
                  {preview.notes.map((n, i) => <Typography key={i} variant="caption" color="text.secondary">{n}</Typography>)}
                  {preview.warning && <Alert severity="warning" sx={{ py: 0 }}>{preview.warning}</Alert>}
                </>
              )}
            </>
          )}
        </Box>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={!valid || saving || !processingEnabled} onClick={save}>
          {status === 'Approved' ? 'Create and approve' : 'Create'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
