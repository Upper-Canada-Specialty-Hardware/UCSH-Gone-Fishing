import { ReactNode } from 'react';
import { HashRouter, Routes, Route, Navigate, useLocation, useParams, useSearchParams } from 'react-router-dom';
import { Container } from '@mui/material';
import { ColorModeProvider } from './colorMode';
import TopBar from './components/TopBar';
import EmployeeDashboard from './pages/EmployeeDashboard';
import ManagerDashboard from './pages/ManagerDashboard';
import AdminDashboard from './pages/AdminDashboard';
import RequestPage from './pages/RequestPage';
import { canSeeTeam, loadSession, saveLinkSession, setAfterSignIn, setSignInNotice } from './api/intake';

/**
 * The frame around a signed-in page: the top bar with the page tabs, then the
 * page in a centred column.
 *
 * @param props.children - The page.
 * @returns The framed page.
 */
function Framed({ children }: { children: ReactNode }) {
  return (
    <>
      <TopBar nav />
      <Container maxWidth="lg" sx={{ py: { xs: 2, sm: 4 } }}>{children}</Container>
    </>
  );
}

/**
 * A page that needs a sign-in. Without one, it remembers where the person was
 * going and sends them to the landing page, which brings them back after the
 * code. My team also needs the manager role; anyone else goes to My requests.
 *
 * @param props.children - The page.
 * @param props.team - True for My team (managers only).
 * @returns The page, or a redirect.
 */
function SignedIn({ children, team }: { children: ReactNode; team?: boolean }) {
  const { pathname } = useLocation();
  const session = loadSession();
  if (!session) {
    setAfterSignIn(pathname);                          // back here after the code
    setSignInNotice(team ? 'Sign in to open My team.' : pathname.startsWith('/my/request')
      ? 'Sign in to make a request.' : 'Sign in to see your requests.');
    return <Navigate to="/" replace />;
  }
  if (team && !canSeeTeam(session)) return <Navigate to="/my" replace />;
  return <>{children}</>;
}

/**
 * The public address everyone bookmarks. Signed out it is the sign-in; signed
 * in it moves straight on to My requests.
 *
 * @returns The landing page, or a redirect.
 */
function Landing() {
  return loadSession() ? <Navigate to="/my" replace /> : <RequestPage />;
}

/**
 * An emailed dashboard link (#/dashboard?token=...). Keeps the link's sign-in
 * for this tab and opens the page it was sent for: My requests, My team, or
 * the admin dashboard.
 *
 * @returns A redirect.
 */
function LinkSignIn() {
  const [params] = useSearchParams();
  const token = params.get('token');
  const role = params.get('role');
  const uid = params.get('uid');
  const exp = params.get('exp');
  if (!token || !role || !uid || !exp) {
    // An old or cut-off link: an existing sign-in still works, otherwise sign in.
    return <Navigate to="/" replace />;
  }
  // A supervisor's "add this new hire" email link carries the person's
  // details; keep them for the Add Employee tab to prefill.
  const addEmail = params.get('add_email');
  if (addEmail) {
    sessionStorage.setItem('add_employee_prefill', JSON.stringify({
      title: params.get('add_name') || '',
      email_address: addEmail,
      location: params.get('add_location') || '',
    }));
  }
  saveLinkSession({ token, role, uid, exp });
  const to = role === 'admin' ? '/admin' : role === 'manager' ? '/team' : '/my';
  return <Navigate to={to} replace />;
}

/**
 * An old dashboard address (#/employee/..., #/manager/...) moved to its new
 * name, keeping the tab.
 *
 * @param props.to - The new base path.
 * @returns A redirect.
 */
function Moved({ to }: { to: string }) {
  const { tab } = useParams();
  return <Navigate to={tab ? `${to}/${tab}` : to} replace />;
}

/**
 * The page shown when a sign-in is refused (#/expired from older code): back
 * to the landing page with a note.
 *
 * @returns A redirect.
 */
function Ended() {
  setSignInNotice('Your sign-in has ended. Enter your email for a new code.');
  return <Navigate to="/" replace />;
}

/**
 * One public address for everyone. Before sign-in, #/ is the landing page.
 * After the emailed code, staff move to #/my (their requests) and
 * #/my/request (a new one), managers also to #/team. People not on staff yet
 * go to #/new-hire. The admin dashboard stays its own bookmark at #/admin.
 *
 * @returns The app.
 */
export default function App() {
  return (
    <ColorModeProvider>
      <HashRouter>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/request" element={<Navigate to="/my/request" replace />} />
          <Route path="/new-hire" element={<RequestPage />} />
          <Route path="/my/request" element={<SignedIn><TopBar nav /><RequestPage /></SignedIn>} />
          {/* The open tab or screen is part of the url, so it survives a refresh and can be shared. */}
          <Route path="/my/:tab?" element={<SignedIn><Framed><EmployeeDashboard /></Framed></SignedIn>} />
          <Route path="/team/:tab?" element={<SignedIn team><Framed><ManagerDashboard /></Framed></SignedIn>} />
          <Route path="/admin/:screen?" element={<AdminDashboard />} />
          <Route path="/dashboard" element={<LinkSignIn />} />
          <Route path="/employee/:tab?" element={<Moved to="/my" />} />
          <Route path="/manager/:tab?" element={<Moved to="/team" />} />
          <Route path="/expired" element={<Ended />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </HashRouter>
    </ColorModeProvider>
  );
}
