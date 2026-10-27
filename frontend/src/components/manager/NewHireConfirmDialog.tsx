import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, Link, Stack, Typography,
} from '@mui/material';
import type { HeldWaiting } from '../../api/client';
import { askedAgo } from '../../utils/requestText';

interface Props {
  /** The held new hire to confirm, or null to close the dialog. */
  item: HeldWaiting | null;
  /**
   * Whether the request matched one existing staff record (its `existing_id`):
   * true opens that record as a profile edit ("Update ..."); false adds a new
   * one ("Add ..."). Both go through the same Add-employee held-for-add flow.
   */
  matched: boolean;
  /** Whether writes are on; the add/decline actions are disabled when off. */
  processingEnabled: boolean;
  /** Whether a decline is in flight, so the buttons lock until it returns. */
  busy: boolean;
  /** Open the Add-employee held-for-add flow for this new hire (addHeldNewHire). */
  onAdd: (item: HeldWaiting) => void;
  /** Hand the request back as "not on my team" (declineHeldNewHire). */
  onDecline: (item: HeldWaiting) => void;
  /**
   * Open the matched member's normal profile instead, when they are on the
   * manager's team (#406). Omitted for a new-hire placeholder with no team card.
   */
  onOpenProfile?: () => void;
  /** Close the dialog without acting. */
  onClose: () => void;
}

/**
 * The one confirm step a manager takes before acting on a held new hire (#406).
 *
 * Anyone can type any name on the public request page, so before a request is
 * turned into a real one the manager must confirm it truly came from the person
 * named. Shared by the Today tab's "New hires waiting on you" panel and the Team
 * tab's flagged and placeholder cards, so every path behaves the same. It shows
 * what the person typed, a clear warning, a required confirmation checkbox, and
 * the two actions the supervisor's email offers: update/add (the held-for-add
 * flow) and "Not on my team" (the decline, behind its own are-you-sure). A
 * matched member also gets an "Open profile" shortcut.
 *
 * @param props - See {@link Props}.
 * @returns The confirm dialog, open whenever `item` is set.
 */
export default function NewHireConfirmDialog({
  item, matched, processingEnabled, busy, onAdd, onDecline, onOpenProfile, onClose,
}: Props) {
  // The person must tick the box before either action is allowed.
  const [confirmed, setConfirmed] = useState(false);
  // The "Not on my team" button reveals an are-you-sure before it declines.
  const [declining, setDeclining] = useState(false);
  // A fresh item (another card opened) starts from an unticked, non-declining state.
  useEffect(() => { setConfirmed(false); setDeclining(false); }, [item?.id]);

  if (!item) return null;                                 // closed: nothing to render
  const name = item.name;                                 // the name as typed on the request page
  const addLabel = matched ? `Update ${name}'s profile` : `Add ${name}`;   // profile edit vs new record

  /** Run the add/profile-edit flow for this new hire, then close. */
  const doAdd = () => { onAdd(item); onClose(); };
  /** Hand the request back, then close (the panel drops the row and re-reads). */
  const doDecline = () => { onDecline(item); onClose(); };

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{matched ? `${name} is already on staff` : `Is this request really from ${name}?`}</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={1.5}>
          {/* What the person typed on the request page, so the manager can judge it. */}
          <Box sx={{ display: 'grid', gap: 0.25 }}>
            <Typography sx={{ fontWeight: 700, wordBreak: 'break-word' }}>{name}</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ wordBreak: 'break-word' }}>{item.email}</Typography>
            {item.department && <Typography variant="body2" color="text.secondary">{item.department}</Typography>}
            <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>{item.summary}</Typography>
            <Typography variant="caption" color="text.secondary">{askedAgo({ Created: item.created_at })}</Typography>
          </Box>

          {/* A name already on staff: most likely that person on the wrong email.
              Say so first, so nobody "adds" someone already on the team. */}
          {matched && (
            <Alert severity="info">
              <strong>{name} is already in the Staff Directory.</strong> This request came from {item.email},
              which is not the email on their record. Most likely they signed in with the wrong email.{' '}
              <strong>You do not need to add or update them.</strong> Ask them to sign in with their work email
              and send the request again, then ask an admin to cancel this one.
            </Alert>
          )}

          {/* The warning: a typed name is not proof of who sent it. */}
          <Alert severity="warning">
            Anyone can type a name on the request page. Only continue if you are sure this request came from {name}
            {matched ? ' and their email has really changed' : ''}.
          </Alert>

          {/* The required confirmation; both actions stay locked until it is ticked. */}
          <FormControlLabel
            control={<Checkbox checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />}
            label={`I confirm this request came from ${name}`}
          />

          {/* The decline's own are-you-sure, shown only once "Not on my team" is pressed. */}
          {declining && (
            <Alert severity="info" sx={{ display: 'grid', gap: 1 }}>
              <span>Ask {name} to pick another supervisor? Their request stays the same.</span>
              <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                <Button size="small" color="error" variant="contained"
                  disabled={!processingEnabled || busy} onClick={doDecline}>
                  Yes, not on my team
                </Button>
                <Button size="small" onClick={() => setDeclining(false)} disabled={busy}>Back</Button>
              </Box>
            </Alert>
          )}

          {/* A matched member keeps the normal way into their profile. */}
          {matched && onOpenProfile && (
            <Box>
              <Link component="button" variant="body2" onClick={() => { onOpenProfile(); onClose(); }}>
                Open {name}&apos;s profile instead
              </Link>
            </Box>
          )}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ flexWrap: 'wrap', gap: 1 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        {/* The email's two buttons: hand back, or confirm and continue. */}
        {!declining && (
          <Button color="error" variant="outlined" disabled={busy} onClick={() => setDeclining(true)}>
            Not on my team
          </Button>
        )}
        {/* For a name already on staff the edit is the rare case, so it is the quieter button. */}
        <Button
          variant={matched ? 'outlined' : 'contained'}
          disabled={!confirmed || !processingEnabled || busy}
          onClick={doAdd}
        >
          {addLabel}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
