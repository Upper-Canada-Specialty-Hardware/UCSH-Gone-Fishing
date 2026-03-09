import { useEffect, useState } from 'react';
import { Box, Typography, Paper, CircularProgress, Alert } from '@mui/material';
import BalanceCards from '../components/BalanceCards';
import RequestHistory from '../components/RequestHistory';
import { getMyBalances, getMyRequests } from '../api/client';

export default function EmployeeDashboard() {
  const [balances, setBalances] = useState<any>(null);
  const [employee, setEmployee] = useState<any>(null);
  const [requests, setRequests] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    Promise.all([getMyBalances(), getMyRequests()])
      .then(([balRes, reqRes]) => {
        setBalances(balRes.data.balances);
        setEmployee(balRes.data.employee);
        setRequests(reqRes.data.requests || []);
      })
      .catch((err) => setError(err.response?.data?.detail || 'Failed to load data'))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
        <CircularProgress />
      </Box>
    );
  }

  if (error) {
    return <Alert severity="error" sx={{ m: 2 }}>{error}</Alert>;
  }

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 3, fontWeight: 600 }}>
        Welcome, {employee?.name || 'Employee'}
      </Typography>

      <BalanceCards balances={balances} />

      <Paper sx={{ mt: 4, p: 3 }}>
        <Typography variant="h6" sx={{ mb: 2 }}>
          My Requests
        </Typography>
        <RequestHistory requests={requests} />
      </Paper>
    </Box>
  );
}
