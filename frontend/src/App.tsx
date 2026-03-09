import { useEffect, useState } from 'react';
import { HashRouter, Routes, Route, Navigate, useSearchParams, useNavigate } from 'react-router-dom';
import { ThemeProvider, createTheme, CssBaseline, AppBar, Toolbar, Typography, Container, Box } from '@mui/material';
import EmployeeDashboard from './pages/EmployeeDashboard';
import ManagerDashboard from './pages/ManagerDashboard';
import AdminDashboard from './pages/AdminDashboard';
import Expired from './pages/Expired';

const theme = createTheme({
  typography: {
    fontFamily: '"Inter", "Segoe UI", sans-serif',
  },
  palette: {
    primary: { main: '#1e40af' },
    success: { main: '#16a34a' },
    error: { main: '#dc2626' },
  },
});

function AuthHandler() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const token = searchParams.get('token');
    const role = searchParams.get('role');
    const uid = searchParams.get('uid');
    const exp = searchParams.get('exp');

    if (token && role && uid && exp) {
      sessionStorage.setItem('dashboard_token', token);
      sessionStorage.setItem('dashboard_role', role);
      sessionStorage.setItem('dashboard_uid', uid);
      sessionStorage.setItem('dashboard_exp', exp);
      navigate(`/${role}`, { replace: true });
      return;
    }

    // Check if we already have stored credentials
    const storedRole = sessionStorage.getItem('dashboard_role');
    if (storedRole) {
      navigate(`/${storedRole}`, { replace: true });
    } else {
      navigate('/expired', { replace: true });
    }
    setReady(true);
  }, [searchParams, navigate]);

  if (!ready) return null;
  return null;
}

export default function App() {
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <HashRouter>
        <AppBar position="static" sx={{ mb: 3 }}>
          <Toolbar>
            <Typography variant="h6" sx={{ fontWeight: 600 }}>
              UCSH Gone Fishing
            </Typography>
          </Toolbar>
        </AppBar>
        <Container maxWidth="lg" sx={{ pb: 4 }}>
          <Routes>
            <Route path="/dashboard" element={<AuthHandler />} />
            <Route path="/employee" element={<EmployeeDashboard />} />
            <Route path="/manager" element={<ManagerDashboard />} />
            <Route path="/admin" element={<AdminDashboard />} />
            <Route path="/expired" element={<Expired />} />
            <Route path="*" element={<Navigate to="/expired" replace />} />
          </Routes>
        </Container>
        <Box component="footer" sx={{ textAlign: 'center', py: 2, color: 'text.secondary', fontSize: 12 }}>
          UCSH Leave Management System
        </Box>
      </HashRouter>
    </ThemeProvider>
  );
}
