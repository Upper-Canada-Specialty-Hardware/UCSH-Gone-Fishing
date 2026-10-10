import { ReactNode, useEffect, useState } from 'react';
import { HashRouter, Routes, Route, Navigate, useSearchParams, useNavigate } from 'react-router-dom';
import { Container } from '@mui/material';
import { ColorModeProvider } from './colorMode';
import TopBar from './components/TopBar';
import EmployeeDashboard from './pages/EmployeeDashboard';
import ManagerDashboard from './pages/ManagerDashboard';
import AdminDashboard from './pages/AdminDashboard';
import Expired from './pages/Expired';
import RequestPage from './pages/RequestPage';

/**
 * The frame around a page: the top bar, then the page in a centred column.
 *
 * @param props.children - The page.
 * @returns The framed page.
 */
function Framed({ children }: { children: ReactNode }) {
  return (
    <>
      <TopBar />
      <Container maxWidth="lg" sx={{ py: { xs: 2, sm: 4 } }}>{children}</Container>
    </>
  );
}

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
      // A supervisor's "add this new hire" email link carries the person's
      // details; keep them for the Add Employee tab to prefill.
      const addEmail = searchParams.get('add_email');
      if (addEmail) {
        sessionStorage.setItem('add_employee_prefill', JSON.stringify({
          title: searchParams.get('add_name') || '',
          email_address: addEmail,
          location: searchParams.get('add_location') || '',
        }));
      }
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
    <ColorModeProvider>
      <HashRouter>
        <Routes>
          {/* The public request page is the landing page: bookmarks, links and QR codes. */}
          <Route path="/" element={<RequestPage />} />
          <Route path="/request" element={<RequestPage />} />
          <Route path="/dashboard" element={<AuthHandler />} />
          <Route path="/employee/:tab?" element={<Framed><EmployeeDashboard /></Framed>} />
          {/* The open tab or screen is part of the url, so it survives a refresh and can be shared. */}
          <Route path="/manager/:tab?" element={<Framed><ManagerDashboard /></Framed>} />
          <Route path="/admin/:screen?" element={<AdminDashboard />} />
          <Route path="/expired" element={<Framed><Expired /></Framed>} />
          <Route path="*" element={<Navigate to="/expired" replace />} />
        </Routes>
      </HashRouter>
    </ColorModeProvider>
  );
}
