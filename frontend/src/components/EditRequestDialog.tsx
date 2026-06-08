import { useState, useEffect, useMemo } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions,
  TextField, MenuItem, Button, Stack, Typography, Alert,
} from '@mui/material';

interface Props {
  open: boolean;
  item: any | null;
  onClose: () => void;
  onSave: (payload: any) => Promise<void>;
}

const LEAVE_TYPES = [
  'Vacation',
  'Sick or Personal Day',
  'Half Day or Partial Day Off',
  'Bereavement',
  'Jury Duty',
];

const CARRYOVER_TYPES = ['Carry Over', 'Payout'];

function isoDate(value: any): string {
  if (!value) return '';
  const s = String(value);
  return s.length >= 10 ? s.slice(0, 10) : s;
}

function weekdaysBetween(start: string, end: string): number {
  if (!start || !end) return 0;
  const s = new Date(start);
  const e = new Date(end);
  if (Number.isNaN(s.getTime()) || Number.isNaN(e.getTime()) || s > e) return 0;
  let count = 0;
  const cur = new Date(s);
  while (cur <= e) {
    const day = cur.getUTCDay();
    if (day !== 0 && day !== 6) count += 1;
    cur.setUTCDate(cur.getUTCDate() + 1);
  }
  return count;
}

export default function EditRequestDialog({ open, item, onClose, onSave }: Props) {
  const [days, setDays] = useState<string>('');
  const [hours, setHours] = useState<string>('');
  const [leaveType, setLeaveType] = useState<string>('');
  const [startDate, setStartDate] = useState<string>('');
  const [endDate, setEndDate] = useState<string>('');
  const [title, setTitle] = useState<string>('');
  const [coType, setCoType] = useState<string>('');
  const [reason, setReason] = useState<string>('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!item) return;
    setError(null);
    setReason('');
    setDays(item.Days != null ? String(item.Days) : '');
    setHours(item.Hours != null ? String(item.Hours) : '');
    setLeaveType(item.LeaveType || '');
    setStartDate(isoDate(item.StartDate));
    setEndDate(isoDate(item.EndDate));
    setTitle(item.Title || '');
    setCoType(item.TypeofRequest || '');
  }, [item]);

  const requestType: string = item?.request_type || '';

  const weekdayHint = useMemo(() => {
    if (requestType !== 'leave') return null;
    const n = weekdaysBetween(startDate, endDate);
    return n > 0 ? `${n} weekday${n === 1 ? '' : 's'} between selected dates (excludes weekends, does not subtract holidays/half-Fridays)` : null;
  }, [requestType, startDate, endDate]);

  const handleSave = async () => {
    setError(null);
    if (!reason.trim()) {
      setError('Reason is required');
      return;
    }
    let payload: any;
    if (requestType === 'leave') {
      if (!leaveType) { setError('Leave Type is required'); return; }
      if (!startDate || !endDate) { setError('Dates are required'); return; }
      if (startDate > endDate) { setError('Start Date must be on or before End Date'); return; }
      const d = Number(days);
      if (!Number.isFinite(d) || d < 0) { setError('Days must be a non-negative number'); return; }
      payload = { Days: d, LeaveType: leaveType, StartDate: startDate, EndDate: endDate, reason };
    } else if (requestType === 'overtime') {
      if (!startDate) { setError('Date is required'); return; }
      const h = Number(hours);
      if (!Number.isFinite(h) || h <= 0) { setError('Hours must be a positive number'); return; }
      payload = { Hours: h, StartDate: startDate, Title: title, reason };
    } else if (requestType === 'carryover-payout') {
      if (!coType) { setError('Request Type is required'); return; }
      const d = Number(days);
      if (!Number.isFinite(d) || d <= 0) { setError('Days must be a positive number'); return; }
      payload = { TypeofRequest: coType, Days: d, reason };
    } else {
      setError(`Unknown request type: ${requestType}`);
      return;
    }
    setSaving(true);
    try {
      await onSave(payload);
      onClose();
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const titleLabel = useMemo(() => {
    if (requestType === 'leave') return `Edit Leave Request #${item?.id ?? ''}`;
    if (requestType === 'overtime') return `Edit Overtime Request #${item?.id ?? ''}`;
    if (requestType === 'carryover-payout') return `Edit Carry Over / Payout Request #${item?.id ?? ''}`;
    return 'Edit Request';
  }, [requestType, item]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{titleLabel}</DialogTitle>
      <DialogContent>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        <Stack spacing={2} sx={{ mt: 1 }}>
          {requestType === 'leave' && (
            <>
              <TextField
                select label="Leave Type" value={leaveType}
                onChange={(e) => setLeaveType(e.target.value)} fullWidth
              >
                {LEAVE_TYPES.map((t) => (
                  <MenuItem key={t} value={t}>{t}</MenuItem>
                ))}
              </TextField>
              <Stack direction="row" spacing={2}>
                <TextField
                  label="Start Date" type="date" value={startDate}
                  onChange={(e) => setStartDate(e.target.value)} fullWidth
                  InputLabelProps={{ shrink: true }}
                />
                <TextField
                  label="End Date" type="date" value={endDate}
                  onChange={(e) => setEndDate(e.target.value)} fullWidth
                  InputLabelProps={{ shrink: true }}
                />
              </Stack>
              <TextField
                label="Days" type="number" value={days}
                onChange={(e) => setDays(e.target.value)} fullWidth
                inputProps={{ step: 0.5, min: 0 }}
                helperText={weekdayHint || ' '}
              />
            </>
          )}
          {requestType === 'overtime' && (
            <>
              <TextField
                label="Date" type="date" value={startDate}
                onChange={(e) => setStartDate(e.target.value)} fullWidth
                InputLabelProps={{ shrink: true }}
              />
              <TextField
                label="Hours" type="number" value={hours}
                onChange={(e) => setHours(e.target.value)} fullWidth
                inputProps={{ step: 0.5, min: 0 }}
              />
              <TextField
                label="Details" value={title}
                onChange={(e) => setTitle(e.target.value)} fullWidth multiline rows={2}
              />
            </>
          )}
          {requestType === 'carryover-payout' && (
            <>
              <TextField
                select label="Request Type" value={coType}
                onChange={(e) => setCoType(e.target.value)} fullWidth
              >
                {CARRYOVER_TYPES.map((t) => (
                  <MenuItem key={t} value={t}>{t}</MenuItem>
                ))}
              </TextField>
              <TextField
                label="Days" type="number" value={days}
                onChange={(e) => setDays(e.target.value)} fullWidth
                inputProps={{ step: 0.5, min: 0 }}
              />
            </>
          )}
          <TextField
            label="Reason for edit (required)" value={reason}
            onChange={(e) => setReason(e.target.value)} fullWidth multiline rows={2}
            helperText="Recorded in the request audit log."
          />
          <Typography variant="caption" color="text.secondary">
            Saving will send a fresh approval email and invalidate any previous one.
          </Typography>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={saving}>Cancel</Button>
        <Button onClick={handleSave} variant="contained" disabled={saving}>
          {saving ? 'Saving…' : 'Save & Re-send Email'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
