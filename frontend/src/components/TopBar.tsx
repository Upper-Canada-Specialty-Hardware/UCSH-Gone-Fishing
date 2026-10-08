import { Box, Button, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import DesktopWindowsOutlinedIcon from '@mui/icons-material/DesktopWindowsOutlined';
import LightModeOutlinedIcon from '@mui/icons-material/LightModeOutlined';
import DarkModeOutlinedIcon from '@mui/icons-material/DarkModeOutlined';
import MenuIcon from '@mui/icons-material/Menu';
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

/**
 * The bar across the top of every dashboard.
 *
 * @param props.onMenu - When given, a menu button shows on small screens (the admin sidebar).
 * @returns The bar.
 */
export default function TopBar({ onMenu }: { onMenu?: () => void }) {
  return (
    <Box
      component="header"
      sx={{
        position: 'sticky', top: 0, zIndex: 20,
        display: 'flex', alignItems: 'center', gap: 1.5,
        px: { xs: 2, sm: 3 }, height: 56,
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
      <Typography sx={{ fontWeight: 700 }}>{APP_NAME}</Typography>
      <Box sx={{ flex: 1 }} />
      <ColorModeButton />
    </Box>
  );
}
