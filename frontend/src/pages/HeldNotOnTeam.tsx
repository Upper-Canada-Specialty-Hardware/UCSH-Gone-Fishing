import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Alert, Box, Button, CircularProgress, Paper, Stack, Typography,
} from '@mui/material';
import { confirmNotOnTeam, getNotOnTeamInfo, NotOnTeamInfo } from '../api/intake';

/**
 * The public "Not on my team" page (#483).
 *
 * Reached from the link in a supervisor's "add this new hire" email, which now
 * points at the public site (``#/held/not-on-team?token=...``) rather than the
 * backend. It reads the request over the JSON endpoints and, while the request
 * is still theirs to decline, shows a confirm button that hands it back; a link
 * opened late says what has already happened. It shows the same outcomes the
 * backend HTML pages do (done, already handled, no longer needed, already done,
 * bad link), so the signed GET/POST HTML routes can keep serving older emails.
 *
 * @returns The page.
 */
export default function HeldNotOnTeam() {
  const [params] = useSearchParams();                  // the token rides in the hash query
  const token = params.get('token') || '';
  const [info, setInfo] = useState<NotOnTeamInfo | null>(null);   // the request's state, once read
  const [loading, setLoading] = useState(true);        // the first read is in flight
  const [busy, setBusy] = useState(false);             // the confirm POST is in flight
  const [error, setError] = useState('');              // a transient failure (e.g. processing off)

  // Read what has happened to the request once, on mount. A missing or forged
  // token reads as "invalid", the same as the server's bad-link screen.
  useEffect(() => {
    let live = true;
    if (!token) { setInfo(invalid()); setLoading(false); return; }   // no token at all
    getNotOnTeamInfo(token)
      .then(({ data }) => { if (live) setInfo(data); })
      .catch(() => { if (live) setInfo(invalid()); })   // unreachable: show the neutral bad-link screen
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [token]);

  /** Hand the request back, then show whatever state the server reports. */
  const confirm = async () => {
    setError(''); setBusy(true);
    try {
      const { data } = await confirmNotOnTeam(token);   // guarded + idempotent on the server
      setInfo(data);                                    // "done", or what it had already become
    } catch (err: any) {
      // 503 while processing is off: keep the confirm button and ask them to retry.
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
const invalid = (): NotOnTeamInfo => ({
  state: 'invalid', name: '', supervisor_name: '', summary: '', request_number: '', set_up_on: '',
});

/**
 * The body for one state: the confirm screen while open, or a message saying
 * what has already happened (#483).
 *
 * @param props.info - The request's current state.
 * @param props.busy - Whether the confirm POST is in flight.
 * @param props.error - A transient failure to show above the button.
 * @param props.onConfirm - Hand the request back.
 * @returns The screen.
 */
function Screen({ info, busy, error, onConfirm }: {
  info: NotOnTeamInfo;
  busy: boolean;
  error: string;
  onConfirm: () => void;
}) {
  const who = info.name || 'This person';                // the new hire's name, or a neutral stand-in
  if (info.state === 'open') {
    // Still theirs: show who it is, the request, and a button to hand it back.
    return (
      <>
        <Typography variant="h5" sx={{ fontWeight: 600 }}>Not on your team?</Typography>
        <Typography>
          {who} picked you as their supervisor for a request they made on the UCSH Out of Office page.
        </Typography>
        {info.summary && (
          <Typography variant="body2" color="text.secondary" sx={{ wordBreak: 'break-word' }}>
            Their request: {info.summary}
          </Typography>
        )}
        <Typography>
          If {who} is not on your team, let us know and we will ask them to pick their supervisor again.
          You do not need to do anything else.
        </Typography>
        {error && <Alert severity="error">{error}</Alert>}
        <Box>
          <Button
            variant="contained" color="error" onClick={onConfirm} disabled={busy}
            startIcon={busy ? <CircularProgress size={16} color="inherit" /> : undefined}
          >
            They are not on my team
          </Button>
        </Box>
      </>
    );
  }
  return <Outcome info={info} who={who} />;
}

/**
 * The final message for a request that is no longer open to decline (#483),
 * matching the backend HTML pages word for word where it can.
 *
 * @param props.info - The request's state.
 * @param props.who - The new hire's name, or a neutral stand-in.
 * @returns The message.
 */
function Outcome({ info, who }: { info: NotOnTeamInfo; who: string }) {
  const { title, body } = outcomeText(info, who);
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
 * @param who - The new hire's name, or a neutral stand-in.
 * @returns ``{ title, body }``.
 */
function outcomeText(info: NotOnTeamInfo, who: string): { title: string; body: string } {
  switch (info.state) {
    case 'done':                                         // just handed back by this click
      return {
        title: 'Thanks for letting us know',
        body: 'We have asked them to pick their supervisor again. You do not need to do anything else.',
      };
    case 'released':                                     // set up since: give the request number
      return {
        title: 'Already handled',
        body: `${who} was set up${info.set_up_on ? ` on ${info.set_up_on}` : ''}.`
          + `${info.request_number ? ` Their request is #${info.request_number}.` : ''}`,
      };
    case 'cancelled':
      return { title: 'No longer needed', body: `${who}'s request was cancelled. Nothing is needed from you.` };
    case 'sending':                                      // on staff now, request on its way
      return { title: 'Already handled', body: `${who} is on staff now and their request is being sent.` };
    case 'declined':                                     // already said so, or it moved off them
      return {
        title: 'Already done',
        body: `You already let us know ${who} is not on your team. Nothing is needed from you.`,
      };
    default:                                             // "invalid": a forged, expired or gone link
      return {
        title: 'This link is not valid',
        body: 'This link could not be used. It may have already been handled, or the request no longer exists.',
      };
  }
}
