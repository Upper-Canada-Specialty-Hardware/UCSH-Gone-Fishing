import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Alert, Box, Button, CircularProgress, Paper, Stack, Typography,
} from '@mui/material';
import { CancelInfo, confirmCancel, getCancelInfo } from '../api/intake';

/**
 * The public "Cancel this request" page (#519).
 *
 * Reached from the link in a staff member's receipt email
 * (``#/cancel?token=...``). It reads the request over the JSON endpoints and,
 * while it is still waiting for a decision, shows a confirm button that cancels
 * it. Opening the link alone changes nothing, so a mail scanner that follows
 * links cannot cancel anything. A link opened late says what the request is now.
 *
 * @returns The page.
 */
export default function CancelRequest() {
  const [params] = useSearchParams();                  // the token rides in the hash query
  const token = params.get('token') || '';
  const [info, setInfo] = useState<CancelInfo | null>(null);   // the request's state, once read
  const [loading, setLoading] = useState(true);        // the first read is in flight
  const [busy, setBusy] = useState(false);             // the confirm POST is in flight
  const [error, setError] = useState('');              // a transient failure (e.g. processing off)

  // Read the request's state once, on mount. A missing or forged token reads as "invalid".
  useEffect(() => {
    let live = true;
    if (!token) { setInfo(invalid()); setLoading(false); return; }   // no token at all
    getCancelInfo(token)
      .then(({ data }) => { if (live) setInfo(data); })
      .catch(() => { if (live) setInfo(invalid()); })   // unreachable: show the neutral bad-link screen
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [token]);

  /** Cancel the request, then show whatever state the server reports. */
  const confirm = async () => {
    setError(''); setBusy(true);
    try {
      const { data } = await confirmCancel(token);      // guarded + idempotent on the server
      setInfo(data);                                    // "done", or what it had already become
    } catch (err: any) {
      // 503 while processing is off: keep the button and ask them to retry.
      const detail = err?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'This could not be done right now. Try again in a few minutes.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box sx={{ minHeight: '100dvh', bgcolor: 'background.default', color: 'text.primary', display: 'grid', placeItems: 'center', p: 2 }}>
      <Paper sx={{ p: { xs: 3, sm: 4 }, width: 'min(32rem, 100%)', display: 'grid', gap: 2 }}>
        {loading ? (
          <Box sx={{ display: 'grid', placeItems: 'center', py: 2 }}><CircularProgress size={28} /></Box>
        ) : (
          <Screen info={info!} busy={busy} error={error} onConfirm={confirm} />
        )}
      </Paper>
    </Box>
  );
}

/** A neutral "invalid" payload, used when there is no token or the read failed. */
const invalid = (): CancelInfo => ({ state: 'invalid', request_number: '', summary: '' });

/**
 * The confirm screen while the request is open, or a message saying what it is now.
 *
 * @param props.info - The request's current state.
 * @param props.busy - Whether the confirm POST is in flight.
 * @param props.error - A transient failure to show above the button.
 * @param props.onConfirm - Cancel the request.
 * @returns The screen.
 */
function Screen({ info, busy, error, onConfirm }: {
  info: CancelInfo;
  busy: boolean;
  error: string;
  onConfirm: () => void;
}) {
  if (info.state === 'open') {
    // Still waiting for a decision: show the request and the button.
    return (
      <>
        <Typography variant="h5" sx={{ fontWeight: 600 }}>Cancel request #{info.request_number}?</Typography>
        {info.summary && (
          <Typography variant="body2" color="text.secondary" sx={{ wordBreak: 'break-word' }}>
            {info.summary}
          </Typography>
        )}
        <Typography>
          It is still waiting for a decision. If you cancel it, your manager is told and nothing comes off your balances.
        </Typography>
        {error && <Alert severity="error">{error}</Alert>}
        <Box>
          <Button
            variant="contained" color="error" onClick={onConfirm} disabled={busy}
            startIcon={busy ? <CircularProgress size={16} color="inherit" /> : undefined}
          >
            Cancel this request
          </Button>
        </Box>
      </>
    );
  }
  const { title, body } = outcomeText(info);
  return (
    <Stack spacing={1.5}>
      <Typography variant="h5" sx={{ fontWeight: 600 }}>{title}</Typography>
      <Typography>{body}</Typography>
    </Stack>
  );
}

/**
 * The heading and sentence for each non-open state.
 *
 * @param info - The request's state.
 * @returns ``{ title, body }``.
 */
function outcomeText(info: CancelInfo): { title: string; body: string } {
  const number = info.request_number ? `#${info.request_number}` : 'This request';
  switch (info.state) {
    case 'done':                                         // just cancelled by this click
      return { title: 'Request cancelled', body: `${number} is cancelled. Your manager has been told.` };
    case 'cancelled':                                    // cancelled before, here or elsewhere
      return { title: 'Already cancelled', body: `${number} was already cancelled. Nothing else is needed.` };
    case 'approved':                                     // approved time off is undone by an admin
      return {
        title: 'Already approved',
        body: `${number} has already been approved. To change approved time off, ask your manager or an admin.`,
      };
    case 'rejected':
      return { title: 'Already decided', body: `${number} was not approved, so there is nothing to cancel.` };
    case 'invalid':                                      // a forged or gone link
      return {
        title: 'This link is not valid',
        body: 'This link could not be used. The request may no longer exist.',
      };
    default:                                             // any other state: it has moved on
      return { title: 'Cannot be cancelled', body: `${number} is no longer waiting for a decision.` };
  }
}
