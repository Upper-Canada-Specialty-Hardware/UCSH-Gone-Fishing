import { FormEvent, ReactNode, useEffect, useState } from 'react';
import { Alert, Box, Button, Paper, TextField, Typography } from '@mui/material';
import { adminSignIn } from '../../api/client';
import { ADMIN_SIGNED_OUT, adminSessionToken, clearAdminSession, saveAdminSession } from '../../api/adminSession';

/**
 * The admin dashboard's password screen (#228). Shows the dashboard once
 * this device holds an admin session, and comes back whenever the server
 * refuses one (expired, or the password changed).
 *
 * The server checks the session on every admin call, so this screen is only
 * the way in, not the protection.
 *
 * @param props.children - The admin dashboard.
 * @returns The password screen or the dashboard.
 */
export default function AdminGate({ children }: { children: ReactNode }) {
  const [signedIn, setSignedIn] = useState(() => !!adminSessionToken());
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  // The client signals a refused session; drop back to this screen.
  useEffect(() => {
    const out = () => { setSignedIn(false); setError('Your admin sign-in ended. Enter the password again.'); };
    window.addEventListener(ADMIN_SIGNED_OUT, out);
    return () => window.removeEventListener(ADMIN_SIGNED_OUT, out);
  }, []);

  /**
   * Send the password; keep the session it returns.
   *
   * @param e - The form submit.
   */
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError('');
    try {
      saveAdminSession((await adminSignIn(password)).data);
      setPassword('');
      setSignedIn(true);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Could not reach the server. Try again.');
    } finally {
      setBusy(false);
    }
  };

  if (signedIn) return <>{children}</>;
  return (
    <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center', px: 2, bgcolor: 'background.default' }}>
      <Paper component="form" onSubmit={submit} sx={{ p: 3, width: '100%', maxWidth: 380, display: 'grid', gap: 2 }}>
        <Box>
          <Typography variant="h6" sx={{ fontWeight: 700 }}>Admin dashboard</Typography>
          <Typography variant="body2" color="text.secondary">Enter the admin password.</Typography>
        </Box>
        <TextField
          label="Password" type="password" autoFocus autoComplete="current-password"
          value={password} onChange={(e) => setPassword(e.target.value)} disabled={busy}
        />
        {error && <Alert severity="error">{error}</Alert>}
        <Button type="submit" variant="contained" disabled={!password || busy}>
          {busy ? 'Checking...' : 'Sign in'}
        </Button>
      </Paper>
    </Box>
  );
}

/** Sign this device out of the admin dashboard and show the password screen. */
export function adminSignOut(): void {
  clearAdminSession();
  window.dispatchEvent(new Event(ADMIN_SIGNED_OUT));
}
