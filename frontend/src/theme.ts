import { createTheme, Theme } from '@mui/material/styles';
import { DUR, EASE, SlideUp, sinkDialog } from './components/Motion';

/**
 * The extra colours the design uses beyond MUI's palette: soft tints for
 * status chips and pills, and the paper/line shades of the warm theme.
 */
export interface Tokens {
  /** Page background behind the paper panels. */
  bg: string;
  /** Hairline borders between rows and around panels. */
  line: string;
  /** Stronger border for inputs and outlined cards. */
  line2: string;
  /** Row hover and quiet fills. */
  hover: string;
  /** Neutral chip and track fill (meters, rings). */
  chip: string;
  /** Pale tint of the primary colour (selected rows, soft buttons). */
  primarySoft: string;
  /** Text on top of the primary colour. */
  primaryInk: string;
  /** Warm second colour (focus rings, holiday shading, highlights). */
  accent: string;
  /** Soft fills behind status text. */
  okSoft: string;
  errSoft: string;
  warnSoft: string;
  infoSoft: string;
  /** Serif face for headings and big numbers. */
  display: string;
}

declare module '@mui/material/styles' {
  interface Theme {
    tokens: Tokens;
  }
  interface ThemeOptions {
    tokens?: Tokens;
  }
}

/** Body text face: DM Sans, loaded in index.html. */
const BODY_FONT = '"DM Sans", "Segoe UI", system-ui, sans-serif';
/** Heading face: Fraunces, a soft serif, loaded in index.html. */
const DISPLAY_FONT = '"Fraunces", Georgia, serif';

/** The warm light palette ("Sunday"): paper, deep green, terracotta. */
const LIGHT = {
  bg: '#f7f3ec', paper: '#fffdf9', ink: '#1f1a14', muted: '#6b6257',
  line: '#ebe4d8', line2: '#d9cfbf', hover: '#f3ede3', chip: '#f1ebe0',
  primary: '#2f5d50', primarySoft: '#e5efe9', primaryInk: '#ffffff', accent: '#e07a5f',
  ok: '#15803d', okSoft: '#e7f5ec', err: '#c2410c', errSoft: '#fdeee8',
  warn: '#a16207', warnSoft: '#fbf3dc', info: '#0369a1', infoSoft: '#e4f1fa',
};

/** The same palette for dark mode: lighter accents on deep brown-black. */
const DARK = {
  bg: '#14120f', paper: '#1d1a16', ink: '#e9e4dc', muted: '#a39a8e',
  line: '#2e2922', line2: '#3e372d', hover: '#25211b', chip: '#2a251e',
  primary: '#8cc5b2', primarySoft: '#1e332c', primaryInk: '#0b1a15', accent: '#ee9a80',
  ok: '#4ade80', okSoft: '#13301f', err: '#fb923c', errSoft: '#3a1d12',
  warn: '#facc15', warnSoft: '#332a0e', info: '#7dd3fc', infoSoft: '#102c3c',
};

/** The colours a page may swap in over the base palette (the request page's moods). */
export interface PrimaryOverride {
  primary: string;
  primarySoft: string;
  primaryInk: string;
  accent?: string;
}

/**
 * Build the app's MUI theme for one colour mode.
 *
 * @param mode - 'light' or 'dark' (already resolved from the system setting).
 * @param override - Optional primary colours to use instead of the deep green.
 * @returns A theme with the warm palette, the two fonts and the extra tokens.
 */
export function buildTheme(mode: 'light' | 'dark', override?: PrimaryOverride): Theme {
  const c = { ...(mode === 'dark' ? DARK : LIGHT), ...override };   // the mode's palette, then any override
  return createTheme({
    palette: {
      mode,
      primary: { main: c.primary, contrastText: c.primaryInk },
      secondary: { main: c.accent },
      success: { main: c.ok },
      error: { main: c.err },
      warning: { main: c.warn },
      info: { main: c.info },
      background: { default: c.bg, paper: c.paper },
      text: { primary: c.ink, secondary: c.muted },
      divider: c.line,
    },
    tokens: {
      bg: c.bg, line: c.line, line2: c.line2, hover: c.hover, chip: c.chip,
      primarySoft: c.primarySoft, primaryInk: c.primaryInk, accent: c.accent,
      okSoft: c.okSoft, errSoft: c.errSoft, warnSoft: c.warnSoft, infoSoft: c.infoSoft,
      display: DISPLAY_FONT,
    },
    shape: { borderRadius: 14 },
    // MUI's own motion (Collapse, Drawer, Menu, Tooltip, Tabs) on the app's tokens (components/Motion.tsx).
    transitions: {
      easing: { easeOut: EASE.out, easeIn: EASE.in, easeInOut: EASE.inOut, sharp: 'cubic-bezier(.4,0,.6,1)' },
      duration: { shortest: DUR.fast, shorter: 200, short: DUR.base, standard: 280, complex: 360, enteringScreen: DUR.base, leavingScreen: DUR.exit },
    },
    typography: {
      fontFamily: BODY_FONT,
      // Headings use the serif; weights stay moderate so they read as calm, not loud.
      h1: { fontFamily: DISPLAY_FONT, fontWeight: 600 },
      h2: { fontFamily: DISPLAY_FONT, fontWeight: 600 },
      h3: { fontFamily: DISPLAY_FONT, fontWeight: 600 },
      h4: { fontFamily: DISPLAY_FONT, fontWeight: 600, letterSpacing: '-0.01em' },
      h5: { fontFamily: DISPLAY_FONT, fontWeight: 600, letterSpacing: '-0.01em' },
      h6: { fontFamily: DISPLAY_FONT, fontWeight: 600 },
      button: { textTransform: 'none', fontWeight: 600 },  // sentence case buttons
    },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          body: { WebkitFontSmoothing: 'antialiased' },
          // The focus ring grows out from the edge instead of appearing.
          ':focus-visible': { outline: `2px solid ${c.accent}`, outlineOffset: 2, animation: 'ooFocusRing .15s ease-out' },
          '@keyframes ooFocusRing': { from: { outlineOffset: '0px', outlineColor: 'transparent' } },
          // Light and dark cross-fade (colorMode.tsx runs it as a view transition).
          '::view-transition-old(root), ::view-transition-new(root)': { animationDuration: '.3s' },
          // Less motion, in one place: every CSS animation and transition ends at once.
          // Spinners keep turning; Motion animations follow MotionConfig instead.
          '@media (prefers-reduced-motion: reduce)': {
            '*, *::before, *::after': {
              animationDuration: '.01ms !important', animationDelay: '0s !important', animationIterationCount: '1 !important',
              transitionDuration: '.01ms !important', transitionDelay: '0s !important', scrollBehavior: 'auto !important',
            },
            '.MuiCircularProgress-root, .MuiCircularProgress-circle': {
              animationDuration: '1.4s !important', animationIterationCount: 'infinite !important',
            },
          },
        },
      },
      MuiPaper: {
        styleOverrides: {
          // Flat paper with a hairline border; dark mode drops MUI's lightening overlay.
          root: { backgroundImage: 'none' },
          outlined: { borderColor: c.line },
        },
        defaultProps: { elevation: 0, variant: 'outlined' },
      },
      MuiCard: { defaultProps: { variant: 'outlined' } },
      MuiButton: {
        styleOverrides: {
          // Buttons dip slightly while pressed, so a tap feels received before the network answers.
          root: ({ theme }) => ({
            borderRadius: 10,
            transition: theme.transitions.create(['background-color', 'box-shadow', 'border-color', 'color', 'transform'], { duration: theme.transitions.duration.short }),
            '&:active': { transform: 'scale(.97)' },
            '@media (prefers-reduced-motion: reduce)': { '&:active': { transform: 'none' } },
          }),
        },
        defaultProps: { disableElevation: true },
      },
      MuiIconButton: {
        styleOverrides: {
          root: {
            '&:active': { transform: 'scale(.94)' },
            '@media (prefers-reduced-motion: reduce)': { '&:active': { transform: 'none' } },
          },
        },
      },
      // Toasts slide up with the spring and drop away quickly.
      MuiSnackbar: { defaultProps: { TransitionComponent: SlideUp } },
      MuiChip: { styleOverrides: { root: { fontWeight: 600 } } },
      MuiAlert: { styleOverrides: { root: { borderRadius: 12 } } },
      MuiTab: { styleOverrides: { root: { textTransform: 'none', fontWeight: 600 } } },
      MuiDialog: {
        // On close the paper sinks back the way it rose while the fade runs.
        defaultProps: { TransitionProps: { onExit: sinkDialog } },
        styleOverrides: {
          // Every dialog rises in a little as it fades in; none when less motion is asked for.
          paper: {
            '@media (prefers-reduced-motion: no-preference)': { animation: 'dialogin .26s ease-out both' },
            '@keyframes dialogin': { from: { opacity: 0, transform: 'translateY(10px) scale(.98)' } },
          },
        },
      },
    },
  });
}
