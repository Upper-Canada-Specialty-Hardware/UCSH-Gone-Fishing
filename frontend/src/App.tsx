import { ReactNode, Suspense, lazy, useEffect, useState } from 'react';
import { HashRouter, Routes, Route, Navigate, useLocation, useParams, useSearchParams } from 'react-router-dom';
import { Container } from '@mui/material';
import { ColorModeProvider } from './colorMode';
import { Enter, MotionProvider } from './components/Motion';
import TopBar from './components/TopBar';
// Each dashboard is its own download, fetched only when its page opens, so the
// landing and request pages do not wait on the admin and manager code.
const EmployeeDashboard = lazy(() => import('./pages/EmployeeDashboard'));
const ManagerDashboard = lazy(() => import('./pages/ManagerDashboard'));
const AdminDashboard = lazy(() => import('./pages/AdminDashboard'));
import AdminGate from './components/admin/AdminGate';
import RequestPage from './pages/RequestPage';
import {
  canSeeTeam, loadSession, refreshSessionRole, saveLinkSession, sessionRoleChecked, setAfterSignIn, setSignInNotice,
} from './api/intake';
import { useVersionReload } from './hooks/useVersionReload';

// sessionStorage flag: this tab already reloaded once for a changed role (#376).
const ROLE_RELOAD_KEY = 'role_refresh_reloaded';

/**
 * Reload the page once per tab after the role changed, so the tabs, the route
 * guards and the dashboards all read the new token. Never reloads twice.
 *
 * @returns True when a reload was started.
 */
function reloadOnceForNewRole(): boolean {
  try {
    if (sessionStorage.getItem(ROLE_RELOAD_KEY)) return false;      // already reloaded in this tab
    sessionStorage.setItem(ROLE_RELOAD_KEY, '1');
  } catch {
    return false;                                                  // storage blocked: no reload loop risk
  }
  window.location.reload();
  return true;
}

/**
 * Mounts the auto-update watcher once for the whole app; renders nothing (#331).
 *
 * @returns null.
 */
function VersionWatcher() {
  useVersionReload();
  return null;
}

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
      {/* A new page eases in under the bar; the bar itself stays put. */}
      <Container maxWidth="lg" sx={{ py: { xs: 2, sm: 4 } }}><Enter>{children}</Enter></Container>
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
  const [checked, setChecked] = useState(sessionRoleChecked());   // role re-checked on this load yet (#376)
  useEffect(() => {
    let live = true;
    refreshSessionRole().then((changed) => {
      if (changed && reloadOnceForNewRole()) return;                // a new role: start fresh with it
      if (live) setChecked(true);
    });
    return () => { live = false; };
  }, []);
  const session = loadSession();
  if (!session) {
    setAfterSignIn(pathname);                          // back here after the code
    setSignInNotice(team ? 'Sign in to open My team.' : pathname.startsWith('/my/request')
      ? 'Sign in to make a request.' : 'Sign in to see your requests.');
    return <Navigate to="/" replace />;
  }
  // Not a manager on the stored token: wait for the role check before sending
  // them to My requests, as they may have become a manager since signing in (#376).
  if (team && !canSeeTeam(session)) return checked ? <Navigate to="/my" replace /> : null;
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
      department: params.get('add_department') || '',
      location: params.get('add_location') || '',       // links emailed before #226
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
      <MotionProvider>
      <VersionWatcher />
      <HashRouter>
        {/* Nothing shows for the moment a dashboard's code is fetched; its own skeleton follows. */}
        <Suspense fallback={null}>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/request" element={<Navigate to="/my/request" replace />} />
          <Route path="/new-hire" element={<RequestPage />} />
          <Route path="/my/request" element={<SignedIn><TopBar nav /><Enter><RequestPage /></Enter></SignedIn>} />
          {/* The open tab or screen is part of the url, so it survives a refresh and can be shared. */}
          <Route path="/my/:tab?" element={<SignedIn><Framed><EmployeeDashboard /></Framed></SignedIn>} />
          <Route path="/team/:tab?" element={<SignedIn team><Framed><ManagerDashboard /></Framed></SignedIn>} />
          {/* The admin password screen comes first (#228); the server checks every admin call too. */}
          <Route path="/admin/:screen?" element={<AdminGate><AdminDashboard /></AdminGate>} />
          <Route path="/dashboard" element={<LinkSignIn />} />
          <Route path="/employee/:tab?" element={<Moved to="/my" />} />
          <Route path="/manager/:tab?" element={<Moved to="/team" />} />
          <Route path="/expired" element={<Ended />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </Suspense>
      </HashRouter>
      </MotionProvider>
    </ColorModeProvider>
  );
}
