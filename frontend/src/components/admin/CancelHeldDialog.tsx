import { useState } from 'react';
import {
  Button, Dialog, DialogActions, DialogContent, DialogContentText, DialogTitle, TextField,
} from '@mui/material';
import { HeldRow } from './requestRows';

/**
 * Ask for a reason before cancelling a held request (#445).
 *
 * Cancelling is now reversible, and the reason is stored and shown on the row, so
 * the admin (or whoever undoes it) can see why it was cancelled. The confirm
 * button stays disabled until a reason is typed.
 *
 * @param props.row - The held request being cancelled, or null when the dialog is closed.
 * @param props.busy - True while the cancel call is running.
 * @param props.onClose - Close without cancelling.
 * @param props.onConfirm - Cancel with the typed reason.
 * @returns The dialog.
 */
export default function CancelHeldDialog({ row, busy, onClose, onConfirm }: {
  row: HeldRow | null;
  busy: boolean;
  onClose: () => void;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState('');            // the typed reason, cleared on each open

  return (
    <Dialog
      open={!!row}
      onClose={busy ? undefined : onClose}             // a running cancel cannot be dismissed
      // Clear the box whenever the dialog opens on a new row, so no stale reason carries over.
      TransitionProps={{ onEnter: () => setReason('') }}
      fullWidth maxWidth="xs"
    >
      <DialogTitle>Cancel this held request?</DialogTitle>
      <DialogContent>
        <DialogContentText sx={{ mb: 2 }}>
          {row ? `${row.name}'s request will be cancelled. You can undo this afterwards.` : ''}
        </DialogContentText>
        <TextField
          label="Reason" autoFocus fullWidth multiline minRows={2}
          value={reason} onChange={(e) => setReason(e.target.value)}
          placeholder="Why is this being cancelled?"
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>Keep it</Button>
        <Button color="error" variant="contained" disabled={busy || !reason.trim()}
          onClick={() => onConfirm(reason.trim())}>
          Cancel request
        </Button>
      </DialogActions>
    </Dialog>
  );
}
