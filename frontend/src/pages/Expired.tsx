import { Box, Typography, Paper, Link } from '@mui/material';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';

export default function Expired() {
  return (
    <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
      <Paper sx={{ p: 4, textAlign: 'center', maxWidth: 400 }}>
        <ErrorOutlineIcon sx={{ fontSize: 64, color: 'error.main', mb: 2 }} />
        <Typography variant="h5" gutterBottom>
          Link Expired
        </Typography>
        <Typography color="text.secondary">
          Your dashboard link has expired. Check a recent email for a new link.
        </Typography>
        <Link href="#/request" sx={{ display: 'inline-block', mt: 2 }}>Make a request instead</Link>
      </Paper>
    </Box>
  );
}
