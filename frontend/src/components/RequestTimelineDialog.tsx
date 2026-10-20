import { useEffect, useState } from 'react';
import {
  Box, Dialog, DialogContent, DialogTitle, Divider, IconButton, Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { LiveScope, RequestTimeline, getRequestTimeline } from '../api/events';

interface Props {
  open: boolean;
  onClose: () => void;
  /** Which timeline endpoint to read: 'me', 'team' or 'admin'. */
  scope: LiveScope;
  /** The url-form request type, or null when nothing is selected. */
  requestType: string | null;
  /** The item id, or null when nothing is selected. */
  itemId: string | null;
}

/**
 * Render an ISO instant as a short local date and time, e.g. "Nov 2, 9:12 AM".
 *
 * @param at - The ISO instant, or null.
 * @returns The formatted time, or "" when there is none.
 */
function timeText(at: string | null): string {
  if (!at) return '';
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

/**
 * Show one request's timeline as plain lines, with the admin field history
 * underneath (#329, #332). Fetches when it opens; updates again if reopened.
 *
 * @param props - See {@link Props}.
 * @returns The dialog.
 */
export default function RequestTimelineDialog({ open, onClose, scope, requestType, itemId }: Props) {
  const [timeline, setTimeline] = useState<RequestTimeline | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!open || !requestType || !itemId) return;
    let cancelled = false;                                 // ignore a late answer after close/reopen
    setTimeline(null);
    setError(false);
    getRequestTimeline(scope, requestType, itemId)
      .then((t) => { if (!cancelled) setTimeline(t); })
      .catch(() => { if (!cancelled) setError(true); });
    return () => { cancelled = true; };
  }, [open, scope, requestType, itemId]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        Request timeline
        <IconButton onClick={onClose} size="small"><CloseIcon /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {error && <Typography color="text.secondary">Could not load the timeline.</Typography>}
        {!error && !timeline && <Typography color="text.secondary">Loading...</Typography>}
        {!error && timeline && timeline.events.length === 0 && (
          <Typography color="text.secondary">No events yet.</Typography>
        )}
        {/* One plain line per event, with its local time underneath. */}
        {timeline?.events.map((e) => (
          <Box key={e.id} sx={{ mb: 1.25 }}>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{e.line}</Typography>
            <Typography variant="caption" color="text.secondary">{timeText(e.at)}</Typography>
          </Box>
        ))}
        {/* The field history, admin scope only (the endpoint returns it only then). */}
        {timeline && timeline.changes.length > 0 && (
          <>
            <Divider sx={{ my: 1.5 }} />
            <Typography variant="subtitle2" sx={{ mb: 1, fontWeight: 700 }}>Field history</Typography>
            {timeline.changes.map((c, i) => (
              <Box key={i} sx={{ mb: 1 }}>
                <Typography variant="body2">
                  <b>{c.field}</b>: {String(c.old ?? '-')} &rarr; {String(c.new ?? '-')}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {[c.actor, c.channel].filter(Boolean).join(' · ')} {timeText(c.at)}
                </Typography>
              </Box>
            ))}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
