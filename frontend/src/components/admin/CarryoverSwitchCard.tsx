import { useEffect, useState } from 'react';
import { Alert, Box, Paper, Switch, Typography } from '@mui/material';
import { getCarryoverPayoutSwitch, setCarryoverPayoutSwitch } from '../../api/client';

/**
 * The on/off switch for carry-over and payout requests.
 *
 * HR takes them at one time of year, so they are closed by default. While
 * closed, the request page does not offer them and the backend refuses new
 * ones; requests already sent keep going through approval. The Microsoft
 * Form is not affected: HR opens and closes it on the bulletin board.
 *
 * @returns A small card for the admin home.
 */
export default function CarryoverSwitchCard() {
  const [open, setOpen] = useState<boolean | null>(null);   // null until loaded
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    getCarryoverPayoutSwitch()
      .then((r) => setOpen(r.data.open))
      .catch(() => setError('Could not check whether carry-over and payout requests are open.'));
  }, []);

  /**
   * Store the new choice, then show what the server kept.
   *
   * @param next - true to open them, false to close them.
   */
  const change = async (next: boolean) => {
    setBusy(true);
    setError('');
    try {
      const r = await setCarryoverPayoutSwitch(next);
      setOpen(r.data.open);                                  // the stored value, not the click
    } catch {
      setError('The change was not saved. Try again.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Paper sx={{ p: 2.5, display: 'grid', gap: 1 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography sx={{ fontWeight: 700 }}>Carry-over and payout requests</Typography>
          <Typography variant="body2" color="text.secondary">
            {open === null ? 'Checking...' : open
              ? 'Open: staff can ask to carry over or cash out vacation days.'
              : 'Closed: the request page does not offer them. Turn on for the carry-over season.'}
          </Typography>
        </Box>
        <Switch
          checked={!!open}
          disabled={open === null || busy}
          onChange={(e) => change(e.target.checked)}
          inputProps={{ 'aria-label': 'Accept carry-over and payout requests' }}
        />
      </Box>
      {error && <Alert severity="error">{error}</Alert>}
    </Paper>
  );
}
