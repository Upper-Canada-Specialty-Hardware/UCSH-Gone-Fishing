import { Alert } from '@mui/material';
import { LiveStatus } from '../hooks/useLiveRequests';

/**
 * A small line shown while the live feed is reconnecting or has fallen back to
 * polling; nothing is shown while it is connecting or live (#328).
 *
 * @param props.status - The live connection status from useLiveRequests.
 * @returns The notice, or null when the feed is healthy.
 */
export default function LiveNotice({ status }: { status: LiveStatus }) {
  if (status !== 'reconnecting' && status !== 'polling') return null;
  return (
    <Alert severity="info" sx={{ py: 0 }}>
      {status === 'reconnecting' ? 'Reconnecting...' : 'Live updates paused; checking every few seconds.'}
    </Alert>
  );
}
