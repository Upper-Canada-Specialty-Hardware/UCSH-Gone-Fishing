import { createContext, ReactNode, useCallback, useContext, useMemo, useState } from 'react';
import { CssBaseline, ThemeProvider, useMediaQuery } from '@mui/material';
import { buildTheme } from './theme';

/** What the person picked: follow the computer, or pin light or dark. */
export type ColorMode = 'system' | 'light' | 'dark';

/** Where the choice is remembered on this device. */
const STORAGE_KEY = 'ucsh-color-mode';

interface ColorModeValue {
  /** The person's choice. */
  mode: ColorMode;
  /** What is showing now, after following the computer's setting. */
  resolved: 'light' | 'dark';
  /** Move to the next choice: system, light, dark, then back. */
  cycle: () => void;
}

const ColorModeContext = createContext<ColorModeValue>({
  mode: 'system',
  resolved: 'light',
  cycle: () => {},
});

/**
 * Read the remembered choice. Storage can be blocked (private windows), so any
 * failure falls back to following the computer.
 *
 * @returns The stored mode, or 'system'.
 */
function readStoredMode(): ColorMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch {
    return 'system';
  }
}

/**
 * Provide the theme for the whole app, light or dark. "System" follows the
 * computer's setting live (the media query re-renders when it changes).
 *
 * @param props.children - The app.
 * @returns The app inside a ThemeProvider and CssBaseline.
 */
export function ColorModeProvider({ children }: { children: ReactNode }) {
  const prefersDark = useMediaQuery('(prefers-color-scheme: dark)', { noSsr: true });
  const [mode, setMode] = useState<ColorMode>(readStoredMode);
  const resolved: 'light' | 'dark' = mode === 'system' ? (prefersDark ? 'dark' : 'light') : mode;

  /** Step to the next mode and remember it on this device. */
  const cycle = useCallback(() => {
    setMode((m) => {
      const next: ColorMode = m === 'system' ? 'light' : m === 'light' ? 'dark' : 'system';
      try { localStorage.setItem(STORAGE_KEY, next); } catch { /* not remembered; still applied */ }
      return next;
    });
  }, []);

  const theme = useMemo(() => buildTheme(resolved), [resolved]);   // rebuild only when the look changes
  const value = useMemo(() => ({ mode, resolved, cycle }), [mode, resolved, cycle]);

  return (
    <ColorModeContext.Provider value={value}>
      <ThemeProvider theme={theme}>
        <CssBaseline enableColorScheme />
        {children}
      </ThemeProvider>
    </ColorModeContext.Provider>
  );
}

/**
 * The current colour mode and the switch.
 *
 * @returns The mode, what is showing, and cycle().
 */
export function useColorMode(): ColorModeValue {
  return useContext(ColorModeContext);
}
