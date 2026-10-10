import { ReactNode } from 'react';
import { Box, Collapse, useMediaQuery } from '@mui/material';
import type { SxProps, Theme } from '@mui/material/styles';

/** The media query people set when they want less motion on screen. */
export const REDUCED_MOTION = '(prefers-reduced-motion: reduce)';

/** How long a decided request takes to leave a list, in ms. */
export const LEAVE_MS = 320;

/**
 * Whether the computer asks for less motion; updates if the setting changes.
 *
 * @returns True when animations should be skipped.
 */
export function useReducedMotion(): boolean {
  return useMediaQuery(REDUCED_MOTION, { noSsr: true });
}

/**
 * How long to wait before taking a decided item out of a list: long enough for
 * it to fade away, or no wait at all when less motion is asked for.
 * Read at call time, for use inside event handlers.
 *
 * @returns Milliseconds.
 */
export function leaveDelay(): number {
  try {
    return window.matchMedia(REDUCED_MOTION).matches ? 0 : LEAVE_MS;
  } catch {
    return 0;                                          // no matchMedia: just remove it
  }
}

/**
 * Plays a short fade and rise when its content first appears. Give it a `key`
 * that changes with the screen or tab, so each change replays it.
 *
 * @param props.children - The screen or tab body.
 * @param props.sx - Extra styles (layout of the children).
 * @returns The wrapper.
 */
export function Enter({ children, sx }: { children: ReactNode; sx?: SxProps<Theme> }) {
  return (
    <Box
      sx={[
        {
          animation: 'enter .32s ease-out both',
          '@keyframes enter': { from: { opacity: 0, transform: 'translateY(6px)' } },
          [`@media ${REDUCED_MOTION}`]: { animation: 'none' },   // no motion when asked
        },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {children}
    </Box>
  );
}

/**
 * Wraps one list item so it can leave smoothly: when `leaving` turns true it
 * fades, slides a little to the side and folds its height away. The caller
 * removes the item from the list after {@link leaveDelay}.
 *
 * @param props.leaving - True once the item has been decided.
 * @param props.children - The item.
 * @returns The wrapper.
 */
export function Leaving({ leaving, children }: { leaving: boolean; children: ReactNode }) {
  const reduced = useReducedMotion();
  const ms = reduced ? 0 : LEAVE_MS;
  return (
    <Collapse in={!leaving} timeout={ms} appear={false}>
      <Box
        sx={{
          transition: `opacity ${ms}ms ease, transform ${ms}ms ease`,
          opacity: leaving ? 0 : 1,
          transform: leaving ? 'translateX(16px)' : 'none',   // drifts off as it fades
        }}
      >
        {children}
      </Box>
    </Collapse>
  );
}
