import { Box, Typography, Paper } from '@mui/material';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';

export default function Expired() {
  return (
    <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
      <Paper sx={{ p: 4, textAlign: 'center', maxWidth: 400 }}>
        <ErrorOutlineIcon sx={{ fontSize: 64, color: '#dc2626', mb: 2 }} />
        <Typography variant="h5" gutterBottom>
          Link Expired
        </Typography>
        <Typography color="text.secondary">
          Your dashboard link has expired. Check a recent email for a new link.
        </Typography>
      </Paper>
    </Box>
  );
}
