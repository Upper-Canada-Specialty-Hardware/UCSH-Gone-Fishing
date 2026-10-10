import { Box, Button, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import DesktopWindowsOutlinedIcon from '@mui/icons-material/DesktopWindowsOutlined';
import LightModeOutlinedIcon from '@mui/icons-material/LightModeOutlined';
import DarkModeOutlinedIcon from '@mui/icons-material/DarkModeOutlined';
import MenuIcon from '@mui/icons-material/Menu';
import { useLocation, useNavigate } from 'react-router-dom';
import { canSeeTeam, clearSession, loadSession } from '../api/intake';
import { useColorMode } from '../colorMode';

/** The app's name, shown in the bar and on the request page. */
export const APP_NAME = 'UCSH Out of Office';

/**
 * The small "OO" mark beside the app name. Colours follow the theme, or the
 * caller's when it sits on a coloured panel.
 *
 * @param props.bg - Mark background (defaults to the primary colour).
 * @param props.fg - Letter colour (defaults to text on primary).
 * @returns The mark.
 */
export function BrandMark({ bg, fg }: { bg?: string; fg?: string }) {
  const theme = useTheme();
  return (
    <Box
      aria-hidden
      sx={{
        width: 28, height: 28, borderRadius: '8px', flex: 'none',
        bgcolor: bg || 'primary.main', color: fg || theme.tokens.primaryInk,
        display: 'grid', placeItems: 'center', fontSize: 12, fontWeight: 800, letterSpacing: '.02em',
        position: 'relative',
        // The terracotta dot: the "away" light.
        '&::after': {
          content: '""', position: 'absolute', top: -2, right: -2, width: 8, height: 8,
          borderRadius: '50%', bgcolor: theme.tokens.accent, border: `2px solid ${bg || theme.palette.background.paper}`,
        },
      }}
    >
      OO
    </Box>
  );
}

/**
 * The light/dark switch: Auto (follows the computer), Light, Dark.
 *
 * @returns A small outlined button showing the current choice.
 */
export function ColorModeButton() {
  const { mode, cycle } = useColorMode();
  const icon = mode === 'system' ? <DesktopWindowsOutlinedIcon fontSize="small" />
    : mode === 'light' ? <LightModeOutlinedIcon fontSize="small" /> : <DarkModeOutlinedIcon fontSize="small" />;
  const label = mode === 'system' ? 'Auto' : mode === 'light' ? 'Light' : 'Dark';
  return (
    <Button
      size="small"
      variant="outlined"
      color="inherit"
      startIcon={icon}
      onClick={cycle}
      title={`Light or dark: ${mode === 'system' ? 'follows your computer' : label}. Click to change.`}
      sx={{ borderRadius: 999, borderColor: 'divider', px: 1.5 }}
    >
      {label}
    </Button>
  );
}

/** The signed-in pages, in the order the tabs show. */
const PAGES = [
  { path: '/my', label: 'My requests' },
  { path: '/my/request', label: 'New request' },
  { path: '/team', label: 'My team', team: true },     // managers only
];

/**
 * The page tabs and the person's name with Sign out, for anyone signed in.
 * My team only shows for a manager. Nothing shows when nobody is signed in.
 *
 * @returns The tabs and account controls, or null.
 */
function SignedInNav() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const session = loadSession();
  if (!session) return null;
  // The open tab: /my/request is its own tab, every other /my page is My requests.
  const current = pathname.startsWith('/my/request') ? '/my/request' : pathname.startsWith('/team') ? '/team' : '/my';
  /** Sign out everywhere and go back to the landing page. */
  const signOut = () => {
    clearSession();
    navigate('/', { replace: true });
  };
  return (
    <>
      <Box
        component="nav"
        aria-label="Pages"
        sx={{
          display: 'flex', alignSelf: 'stretch', overflowX: 'auto', scrollbarWidth: 'none',
          order: { xs: 3, sm: 0 }, width: { xs: '100%', sm: 'auto' }, ml: { xs: -1, sm: 1 },
        }}
      >
        {PAGES.filter((p) => !p.team || canSeeTeam(session)).map((p) => (
          <Button
            key={p.path}
            color="inherit"
            onClick={() => navigate(p.path)}
            aria-current={current === p.path ? 'page' : undefined}
            sx={{
              borderRadius: 0, px: 1.5, minHeight: { xs: 44, sm: 56 }, whiteSpace: 'nowrap', fontWeight: 600,
              color: current === p.path ? 'text.primary' : 'text.secondary',
              borderBottom: 3, borderColor: current === p.path ? 'secondary.main' : 'transparent',
            }}
          >
            {p.label}
          </Button>
        ))}
      </Box>
      <Box sx={{ flex: 1 }} />
      {session.name && (
        <Typography variant="body2" sx={{ fontWeight: 500, display: { xs: 'none', md: 'block' } }}>{session.name}</Typography>
      )}
      <Button size="small" variant="outlined" color="inherit" onClick={signOut} sx={{ borderColor: 'divider' }}>
        Sign out
      </Button>
    </>
  );
}

/**
 * The bar across the top of every page after sign-in, and of the admin dashboard.
 *
 * @param props.onMenu - When given, a menu button shows on small screens (the admin sidebar).
 * @param props.nav - Show the page tabs and Sign out (left off on the admin dashboard, its own page).
 * @returns The bar.
 */
export default function TopBar({ onMenu, nav }: { onMenu?: () => void; nav?: boolean }) {
  return (
    <Box
      component="header"
      sx={{
        position: 'sticky', top: 0, zIndex: 20,
        display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: { xs: 'wrap', sm: 'nowrap' },
        px: { xs: 2, sm: 3 }, minHeight: 56, pt: { xs: nav ? 1 : 0, sm: 0 },
        bgcolor: 'background.paper', borderBottom: 1, borderColor: 'divider',
      }}
    >
      {onMenu && (
        <Button
          onClick={onMenu}
          aria-label="Open the menu"
          color="inherit"
          sx={{ minWidth: 0, p: 0.75, display: { md: 'none' } }}
        >
          <MenuIcon />
        </Button>
      )}
      <BrandMark />
      <Typography sx={{ fontWeight: 700, whiteSpace: 'nowrap' }}>{APP_NAME}</Typography>
      {nav ? <SignedInNav /> : <Box sx={{ flex: 1 }} />}
      <ColorModeButton />
    </Box>
  );
}
