import { FormEvent, useState } from 'react';
import { Alert, Box, Button, Paper, TextField, Typography } from '@mui/material';
import { changeAdminPassword } from '../../api/client';
import { saveAdminSession } from '../../api/adminSession';
import { adminSignOut } from './AdminGate';

/**
 * Change the admin dashboard password, or sign this device out (#228).
 *
 * Changing it signs every other device out; this one gets a fresh session.
 *
 * @returns The card.
 */
export default function AdminPasswordCard() {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [again, setAgain] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);

  const mismatch = !!again && next !== again;                  // the two new ones differ

  /**
   * Send the change; keep the fresh session.
   *
   * @param e - The form submit.
   */
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(''); setDone(false);
    try {
      saveAdminSession((await changeAdminPassword(current, next)).data);
      setCurrent(''); setNext(''); setAgain('');
      setDone(true);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'The password was not changed. Try again.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Paper component="form" onSubmit={submit} sx={{ p: 2.5, display: 'grid', gap: 2, maxWidth: 480 }}>
      <Box>
        <Typography sx={{ fontWeight: 700 }}>Change the admin password</Typography>
        <Typography variant="body2" color="text.secondary">
          Everyone signed in to the admin dashboard on another device is signed out and needs the new password.
        </Typography>
      </Box>
      <TextField label="Current password" type="password" autoComplete="current-password"
        value={current} onChange={(e) => setCurrent(e.target.value)} disabled={busy} />
      <TextField label="New password" type="password" autoComplete="new-password"
        value={next} onChange={(e) => setNext(e.target.value)} disabled={busy} />
      <TextField label="New password again" type="password" autoComplete="new-password"
        value={again} onChange={(e) => setAgain(e.target.value)} disabled={busy}
        error={mismatch} helperText={mismatch ? 'The two new passwords are different.' : ' '} />
      {error && <Alert severity="error">{error}</Alert>}
      {done && <Alert severity="success">Password changed.</Alert>}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        <Button type="submit" variant="contained" disabled={!current || !next.trim() || next !== again || busy}>
          {busy ? 'Saving...' : 'Change password'}
        </Button>
        <Box sx={{ flex: 1 }} />
        <Button onClick={adminSignOut}>Sign out of the admin dashboard</Button>
      </Box>
    </Paper>
  );
}
