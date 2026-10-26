import { Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Typography } from '@mui/material';

/**
 * Shown when an approve or reject is refused by the backend (#486): a centred
 * dialog carrying the backend's reason, in place of a corner snackbar, so the
 * admin or manager cannot miss why the decision did not go through (an overlap
 * conflict, a request that is not ready yet, one that needs attention, or
 * processing switched off). One component, used by both the admin dashboard and
 * the manager dashboard.
 *
 * It stays open until Close (or Escape) is used; a backdrop click does not
 * dismiss it, so the reason is cleared on purpose. The row or card it came from
 * is left untouched (it was not decided), so its buttons work again once this
 * closes.
 *
 * @param props.open - Whether the dialog is shown.
 * @param props.approving - True when an approve was refused, false for a reject; it only sets the wording.
 * @param props.message - The backend's readable reason, shown verbatim as the body.
 * @param props.onClose - Close the dialog; the caller clears its refusal state.
 * @returns The refusal dialog.
 */
export default function DecisionRefusedDialog({ open, approving, message, onClose }: {
  open: boolean;
  approving: boolean;
  message: string;
  onClose: () => void;
}) {
  // The title names which action failed; the verb matches the button pressed.
  const title = approving ? 'This request could not be approved' : 'This request could not be rejected';
  return (
    <Dialog
      open={open}
      // Close on the Close button or Escape only; a backdrop click must not
      // dismiss it, so the reason is read and cleared on purpose (#486).
      onClose={(_, reason) => { if (reason !== 'backdropClick') onClose(); }}
      maxWidth="xs"
      fullWidth                                  // full width within the dialog's own margins, so it reads on a phone
    >
      <DialogTitle>{title}</DialogTitle>
      <DialogContent dividers>
        {/* The backend's detail verbatim (e.g. the overlap "conflict" refusal); wraps at any width. */}
        <Typography sx={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>
          {message}
        </Typography>
      </DialogContent>
      <DialogActions>
        {/* One action, kept reachable with the thumb on a phone. */}
        <Box sx={{ display: 'flex', justifyContent: 'flex-end', width: '100%' }}>
          <Button variant="contained" onClick={onClose} autoFocus>Close</Button>
        </Box>
      </DialogActions>
    </Dialog>
  );
}
