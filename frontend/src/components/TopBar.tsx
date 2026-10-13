import { useEffect, useState } from 'react';
import { Box, Button, Typography } from '@mui/material';
import { m } from 'motion/react';
import { useTheme } from '@mui/material/styles';
import DesktopWindowsOutlinedIcon from '@mui/icons-material/DesktopWindowsOutlined';
import LightModeOutlinedIcon from '@mui/icons-material/LightModeOutlined';
import DarkModeOutlinedIcon from '@mui/icons-material/DarkModeOutlined';
import MenuIcon from '@mui/icons-material/Menu';
import { useLocation, useNavigate } from 'react-router-dom';
import { canSeeTeam, clearSession, loadSession } from '../api/intake';
import { useColorMode } from '../colorMode';
import { KF } from './Motion';

/** The away light pulses on the first mark that mounts, once per visit. */
let pulsed = false;

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
  const [pulse, setPulse] = useState(false);
  useEffect(() => {
    if (pulsed) return;
    pulsed = true;
    setPulse(true);
  }, []);
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
        // On the first visit the light pulses twice, then rests.
        '&::before': pulse ? {
          content: '""', position: 'absolute', top: -2, right: -2, width: 8, height: 8, borderRadius: '50%',
          border: `2px solid ${theme.tokens.accent}`, opacity: 0, animation: `${KF.ping} .9s ease-out .6s 2`,
        } : undefined,
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
      startIcon={
        // The icon turns in as the mode changes.
        <m.span key={mode} initial={{ rotate: -90, scale: 0.6, opacity: 0 }} animate={{ rotate: 0, scale: 1, opacity: 1 }} style={{ display: 'inline-flex' }}>
          {icon}
        </m.span>
      }
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
              color: current === p.path ? 'text.primary' : 'text.secondary', '&:active': { transform: 'none' },
            }}
          >
            {p.label}
            {/* One underline that glides to the open page's tab. */}
            {current === p.path && (
              <m.span
                layoutId="page-tab-underline"
                style={{ position: 'absolute', left: 0, right: 0, bottom: 0, height: 3, borderRadius: '2px 2px 0 0' }}
                className="page-tab-underline"
              />
            )}
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
  // Once the page scrolls under the bar it lifts with a soft shadow, so its edge reads.
  const [raised, setRaised] = useState(false);
  useEffect(() => {
    const onScroll = () => setRaised(window.scrollY > 4);
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);
  return (
    <Box
      component="header"
      sx={(t) => ({
        boxShadow: raised ? `0 6px 18px ${t.palette.mode === 'dark' ? 'rgb(0 0 0 / .45)' : 'rgb(31 26 20 / .08)'}` : 'none',
        transition: t.transitions.create('box-shadow', { duration: t.transitions.duration.short }),
        '& .page-tab-underline': { backgroundColor: t.palette.secondary.main },
        position: 'sticky', top: 0, zIndex: 20,
        display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: { xs: 'wrap', sm: 'nowrap' },
        px: { xs: 2, sm: 3 }, minHeight: 56, pt: { xs: nav ? 1 : 0, sm: 0 },
        bgcolor: 'background.paper', borderBottom: 1, borderColor: 'divider',
      })}
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
