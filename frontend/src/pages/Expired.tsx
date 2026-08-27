import { Box, Typography, Paper, Button } from '@mui/material';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import { Link as RouterLink } from 'react-router-dom';

export default function Expired() {
  return (
    <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
      <Paper sx={{ p: 4, textAlign: 'center', maxWidth: 400 }}>
        <ErrorOutlineIcon sx={{ fontSize: 64, color: '#dc2626', mb: 2 }} />
        <Typography variant="h5" gutterBottom>
          Link Expired
        </Typography>
        <Typography color="text.secondary" sx={{ mb: 3 }}>
          Your dashboard link has expired, or you don't have one yet. Request a
          fresh link by email.
        </Typography>
        {/* Self-service replacement for the old "check a recent email" dead end. */}
        <Button component={RouterLink} to="/login" variant="contained">
          Email me a sign-in link
        </Button>
      </Paper>
    </Box>
  );
}
