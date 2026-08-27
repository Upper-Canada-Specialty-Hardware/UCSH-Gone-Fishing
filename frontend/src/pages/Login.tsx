import { useState } from 'react';
import { Box, Typography, Paper, TextField, Button } from '@mui/material';
import MailOutlineIcon from '@mui/icons-material/MailOutline';
import { requestSignInLink } from '../api/client';

/**
 * Public sign-in page: request a dashboard link by email.
 *
 * Renders a single email field. On submit it asks the backend to email a signed
 * dashboard link, then shows the same confirmation regardless of whether the
 * address was on file (the backend never reveals that), so the page cannot be
 * used to probe who exists.
 *
 * @returns The login page element.
 */
export default function Login() {
  const [email, setEmail] = useState('');            // controlled email input
  const [submitted, setSubmitted] = useState(false); // switch to the confirmation view
  const [loading, setLoading] = useState(false);     // disable controls while in flight

  /**
   * Submit the email and move to the confirmation view.
   *
   * Advances to the confirmation even on a network error, so the page never
   * behaves differently for a known vs unknown address.
   *
   * @param e - The form submit event.
   */
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();                              // keep it a single-page interaction
    setLoading(true);
    try {
      await requestSignInLink(email.trim());         // fire-and-forget; response is generic
    } catch {
      // Swallow: the confirmation is intentionally identical on failure.
    } finally {
      setLoading(false);
      setSubmitted(true);                            // show the neutral confirmation
    }
  };

  if (submitted) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
        <Paper sx={{ p: 4, textAlign: 'center', maxWidth: 420 }}>
          <MailOutlineIcon sx={{ fontSize: 64, color: '#2563eb', mb: 2 }} />
          <Typography variant="h5" gutterBottom>Check your email</Typography>
          <Typography color="text.secondary">
            If that address is on file, a sign-in link is on its way. The link is
            valid for 30 days.
          </Typography>
        </Paper>
      </Box>
    );
  }

  return (
    <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
      <Paper component="form" onSubmit={handleSubmit} sx={{ p: 4, maxWidth: 420, width: '100%' }}>
        <Typography variant="h5" gutterBottom>Sign in</Typography>
        <Typography color="text.secondary" sx={{ mb: 3 }}>
          Enter your UCSH email and we'll send you a link to your dashboard.
        </Typography>
        <TextField
          fullWidth
          type="email"
          label="Work email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          sx={{ mb: 3 }}
        />
        <Button type="submit" variant="contained" fullWidth disabled={loading || !email.trim()}>
          {loading ? 'Sending…' : 'Email me a sign-in link'}
        </Button>
      </Paper>
    </Box>
  );
}
