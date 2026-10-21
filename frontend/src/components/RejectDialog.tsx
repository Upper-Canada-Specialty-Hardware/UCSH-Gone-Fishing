import { useEffect, useState } from 'react';
import {
  Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, TextField, Typography,
} from '@mui/material';

/**
 * The one dialog every reject button opens (#433): it names who and what is
 * being turned down, takes a required reason, and only enables Reject once a
 * non-blank reason is typed. The caller does the actual reject in onConfirm, so
 * this component holds no API knowledge and is shared by the manager Today
 * queue, the manager History table, the admin requests screen (one reason for a
 * whole selection) and the managers-on-vacation screen.
 *
 * @param props.open - Whether the dialog is shown.
 * @param props.personName - The employee whose request is being rejected, when known.
 * @param props.requestLabel - A short description of the request, when known.
 * @param props.count - How many requests this one reason will reject (bulk); 1 or omitted for a single row.
 * @param props.busy - Whether the reject is running; disables the buttons.
 * @param props.onCancel - Close without rejecting.
 * @param props.onConfirm - Reject with the trimmed reason.
 * @returns The reject dialog.
 */
export default function RejectDialog({
  open, personName, requestLabel, count = 1, busy = false, onCancel, onConfirm,
}: {
  open: boolean;
  personName?: string;
  requestLabel?: string;
  count?: number;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState('');                 // the typed reason; required, non-blank

  // The backend caps the reason at 500 characters (it goes verbatim into the
  // result SMS, which Twilio refuses over its limit); show and enforce it here (finding 14).
  const MAX_REASON = 500;

  // Start each open with an empty box, so a previous reason never carries over.
  useEffect(() => { if (open) setReason(''); }, [open]);

  const trimmed = reason.trim();                            // what the backend will store
  const many = count > 1;                                   // a bulk reject shares one reason

  /** Confirm when a reason is present; a no-op guard mirrors the disabled button. */
  const confirm = () => { if (trimmed && !busy) onConfirm(trimmed); };

  return (
    <Dialog open={open} onClose={onCancel} maxWidth="xs" fullWidth>
      <DialogTitle>{many ? `Reject ${count} requests?` : 'Reject this request?'}</DialogTitle>
      <DialogContent dividers>
        {/* Who and what is being rejected, when the caller knows it. */}
        {(personName || requestLabel) && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5, overflowWrap: 'anywhere' }}>
            {many
              ? `This reason is sent to everyone in the selection.`
              : <>{personName}{requestLabel ? ` - ${requestLabel}` : ''}</>}
          </Typography>
        )}
        {/* The reason the employee is told; Reject stays disabled until it is non-blank. */}
        <TextField
          label="Reason for rejecting"
          placeholder="Tell the employee why"
          multiline
          rows={3}
          fullWidth
          required
          autoFocus
          value={reason}
          onChange={(e) => setReason(e.target.value.slice(0, MAX_REASON))}
          inputProps={{ maxLength: MAX_REASON, 'aria-label': 'Reason for rejecting' }}
          helperText={`${reason.length}/${MAX_REASON}`}
          FormHelperTextProps={{ sx: { textAlign: 'right', m: 0, mt: 0.5 } }}
        />
      </DialogContent>
      <DialogActions>
        {/* Keep the two actions reachable with the thumb on a phone. */}
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', justifyContent: 'flex-end', width: '100%' }}>
          <Button onClick={onCancel} disabled={busy}>Cancel</Button>
          <Button variant="contained" color="error" disabled={!trimmed || busy} onClick={confirm}>
            {many ? `Reject ${count}` : 'Reject'}
          </Button>
        </Box>
      </DialogActions>
    </Dialog>
  );
}
