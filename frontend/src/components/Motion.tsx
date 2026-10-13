import { ReactNode, forwardRef, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Box, Slide } from '@mui/material';
import type { SxProps, Theme } from '@mui/material/styles';
import type { TransitionProps } from '@mui/material/transitions';
import { keyframes } from '@emotion/react';
import {
  AnimatePresence, LazyMotion, MotionConfig, animate, domMax, m, useAnimate, useInView, useReducedMotion as useMotionReduced,
} from 'motion/react';

/*
 * The app's one motion vocabulary. Every animation reads its timing from here,
 * so the screens move alike: CSS for one-shot entrances, Motion (motion/react)
 * for exits, shared highlights, reflow, springs and number tweens.
 */

/** The media query people set when they want less motion on screen. */
export const REDUCED_MOTION = '(prefers-reduced-motion: reduce)';

/** Durations in ms: hover and press, entrances, anything leaving, slow draws. */
export const DUR = { fast: 150, base: 240, exit: 160, slow: 400 };

/** CSS easings: settle out, accelerate away, both ends. */
export const EASE = {
  out: 'cubic-bezier(.2,.8,.2,1)',
  in: 'cubic-bezier(.4,0,1,1)',
  inOut: 'cubic-bezier(.4,0,.2,1)',
};

/** The same easings as Motion takes them. */
const EASE_OUT: [number, number, number, number] = [0.2, 0.8, 0.2, 1];
const EASE_IN: [number, number, number, number] = [0.4, 0, 1, 1];

/** The spring: 0.35 s as it reads, no overshoot. Highlights, reflow, panels. */
export const SPRING = { type: 'spring', bounce: 0, visualDuration: 0.35 } as const;
/** A spring with a little bounce, for small pops only (ticks, counts). */
export const POP = { type: 'spring', bounce: 0.25, visualDuration: 0.4 } as const;
/** Leaving: quick and accelerating. */
export const EXIT = { duration: DUR.exit / 1000, ease: EASE_IN };

/** The spring as a CSS easing, for MUI transitions; 650 ms includes the settle. */
export const CSS_SPRING = 'linear(0, 0.0792, 0.2371, 0.4044, 0.5525, 0.6726, 0.7652, 0.8341, 0.8842, 0.9199, 0.9451, 0.9626, 0.9747, 0.9829, 0.9886, 0.9924, 0.9949, 0.9966, 0.9978, 0.9985, 0.999, 1)';
export const CSS_SPRING_MS = 650;

/**
 * Every keyframe the app uses. The keyframes helper gives each a unique name,
 * so two animations can never share one by accident (@keyframes names are global).
 * A drawn SVG shape sets pathLength={1} and strokeDasharray 1, so `draw` fits every length.
 */
export const KF = {
  rise: keyframes`from { opacity: 0; transform: translate(var(--enter-x, 0px), var(--enter-y, 8px)); }`,
  fade: keyframes`from { opacity: 0; }`,
  growX: keyframes`from { transform: scaleX(0); }`,
  growXFade: keyframes`from { transform: scaleX(0); opacity: 0; }`,
  growY: keyframes`from { transform: scaleY(0); }`,
  draw: keyframes`from { stroke-dashoffset: 1; }`,
  pop: keyframes`from { transform: scale(.4); opacity: 0; }`,
  popIn: keyframes`from { transform: scale(0); }`,
  nudge: keyframes`0% { transform: scale(.82); } 60% { transform: scale(1.06); } 100% { transform: none; }`,
  pulse: keyframes`50% { opacity: .4; }`,
  ping: keyframes`from { transform: scale(1); opacity: .7; } to { transform: scale(2.6); opacity: 0; }`,
  drift: keyframes`from { transform: translate(0, 0); } to { transform: translate(-14px, -10px); }`,
  stripes: keyframes`to { background-position: 8.485px 0; }`,
  shake: keyframes`20% { transform: translateX(-7px); } 40% { transform: translateX(6px); } 60% { transform: translateX(-4px); } 80% { transform: translateX(3px); }`,
  empty: keyframes`from { stroke-dashoffset: 0; } to { stroke-dashoffset: 1; }`,
};

/**
 * The rise-in for one item of a list, delayed by its place.
 *
 * @param i - The item's index.
 * @param o.step - Ms between items. @param o.delay - Ms before the first. @param o.max - Items after this share the last delay.
 * @param o.dur - Ms each takes.
 * @returns sx for the item.
 */
export function riseAt(i: number, o: { step?: number; delay?: number; max?: number; dur?: number } = {}) {
  const { step = 45, delay = 0, max = 10, dur = 380 } = o;
  return { animation: `${KF.rise} ${dur}ms ${EASE.out} ${delay + Math.min(i, max) * step}ms both` };
}

/**
 * The same rise for every direct child, one after another, for lists whose
 * children are not mapped in one place.
 *
 * @param o - As {@link riseAt}.
 * @returns sx for the parent.
 */
export function staggerChildren(o: { step?: number; delay?: number; max?: number; dur?: number } = {}) {
  const { step = 45, delay = 0, max = 10, dur = 380 } = o;
  // "* + * + *" matches the third child on: every rule has the same weight, so for
  // each child the last rule that matches (the one for its place) sets its delay.
  const sx: Record<string, object> = { '& > *': { animation: `${KF.rise} ${dur}ms ${EASE.out} ${delay}ms both` } };
  for (let i = 1; i <= max; i++) sx[`& > *${' + *'.repeat(i)}`] = { animationDelay: `${delay + i * step}ms` };
  return sx;
}

/**
 * Whether the computer asks for less motion, read at call time (event handlers).
 *
 * @returns True when animations should be skipped.
 */
export function prefersReduced(): boolean {
  try {
    return window.matchMedia(REDUCED_MOTION).matches;
  } catch {
    return false;
  }
}

/** How long a decided request shows its outcome before it leaves a list, in ms. */
export const LEAVE_MS = 320;

/**
 * How long to wait before taking a decided item out of a list, or no wait at
 * all when less motion is asked for.
 *
 * @returns Milliseconds.
 */
export function leaveDelay(): number {
  return prefersReduced() ? 0 : LEAVE_MS;
}

/** No animation: what a height or width change uses when less motion is asked for. */
export const INSTANT = { duration: 0 };

/**
 * Whether the computer asks for less motion, as a hook. Motion's own setting
 * drops only transforms, so height and width changes check this themselves.
 *
 * @returns True when animations should be skipped.
 */
export function useCalm(): boolean {
  return !!useMotionReduced();
}

/** Hides text from the eye but not from screen readers. */
const SR_ONLY = { position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)', whiteSpace: 'nowrap' } as const;

/**
 * Motion for the whole app: features loaded with the app (loading them later
 * could leave hidden content stuck at its start if the load failed), the spring as the default
 * transition, and transforms dropped (fades kept) when less motion is asked for.
 *
 * @param props.children - The app.
 * @returns The provider.
 */
export function MotionProvider({ children }: { children: ReactNode }) {
  return (
    <LazyMotion features={domMax}>
      <MotionConfig reducedMotion="user" transition={SPRING}>{children}</MotionConfig>
    </LazyMotion>
  );
}

/**
 * Plays a short fade and rise when its content first appears. Give it a `key`
 * that changes with the screen or tab, so each change replays it. With `dir`
 * it slides in from the side the person moved towards instead.
 *
 * @param props.children - The screen or tab body.
 * @param props.dir - 1 when moving right (a later tab), -1 when moving left.
 * @param props.sx - Extra styles (layout of the children).
 * @returns The wrapper.
 */
export function Enter({ children, dir, sx }: { children: ReactNode; dir?: 1 | -1; sx?: SxProps<Theme> }) {
  return (
    <Box
      sx={[
        {
          '--enter-x': dir ? `${dir * 14}px` : '0px',
          '--enter-y': dir ? '0px' : '6px',
          animation: `${KF.rise} ${DUR.base + 80}ms ${EASE.out} both`,
        },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {children}
    </Box>
  );
}

/**
 * Which way the person last moved through an ordered set (tabs, steps).
 *
 * @param index - The current position.
 * @returns 1 after moving to a later one, -1 after moving back.
 */
export function useDirection(index: number): 1 | -1 {
  const prev = useRef(index);
  const dir = useRef<1 | -1>(1);
  if (index !== prev.current) {
    dir.current = index > prev.current ? 1 : -1;
    prev.current = index;
  }
  return dir.current;
}

/**
 * True for the first moments after a page mounts, so numbers count up on
 * arrival only and not on every later render or tab change.
 *
 * @param ms - How long the arrival lasts.
 * @returns Whether the page is still arriving.
 */
export function useArrival(ms = 1500): boolean {
  const [on, setOn] = useState(true);
  useEffect(() => {
    const t = setTimeout(() => setOn(false), ms);
    return () => clearTimeout(t);
  }, [ms]);
  return on;
}

/**
 * The number of decimals a value is written with.
 *
 * @param n - The value.
 * @returns 0 for whole numbers.
 */
function decimalsOf(n: number): number {
  const s = String(n);
  return s.includes('.') ? s.split('.')[1].length : 0;
}

/**
 * A number that counts to its value: from `from` when it first shows, then
 * from wherever it was whenever the value changes. Eases out, never overshoots.
 *
 * @param props.value - The number to show.
 * @param props.from - Where the first count starts; leave out to show the value at once.
 * @param props.format - How to write it (default: as many decimals as the values have).
 * @param props.delay - Seconds before counting.
 * @param props.duration - Seconds the count takes.
 * @returns The number in a span.
 */
export function CountUp({ value, from, format, delay = 0, duration = 0.8 }: {
  value: number; from?: number; format?: (n: number) => string; delay?: number; duration?: number;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const shown = useRef(from ?? value);
  const reduced = useMotionReduced();
  const dec = Math.max(decimalsOf(value), decimalsOf(from ?? value));
  const write = format ?? ((n: number) => String(Number(n.toFixed(dec))));
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const start = shown.current;
    if (reduced || start === value) {
      shown.current = value;
      el.textContent = write(value);
      return;
    }
    const run = animate(start, value, {
      duration, delay, ease: EASE_OUT,
      onUpdate: (v) => { shown.current = v; el.textContent = write(v); },
    });
    return () => run.stop();
  }, [value]);                                         // eslint-disable-line react-hooks/exhaustive-deps
  // Screen readers hear only the final number, never the count on its way there.
  return (
    <span style={{ fontVariantNumeric: 'tabular-nums', position: 'relative' }}>
      <span ref={ref} aria-hidden="true">{write(shown.current)}</span>
      <span style={SR_ONLY}>{write(value)}</span>
    </span>
  );
}

/**
 * Text that swaps with a short slide when it changes: counts in a tab label,
 * a button's label after it worked.
 *
 * @param props.k - Changes whenever the content should swap.
 * @param props.children - The content.
 * @param props.y - How far it slides, in px.
 * @returns The swapping span.
 */
export function Swap({ k, children, y = 5 }: { k: string | number; children: ReactNode; y?: number }) {
  return (
    <span style={{ position: 'relative', display: 'inline-flex', verticalAlign: 'top' }}>
      <AnimatePresence mode="popLayout" initial={false}>
        <m.span
          key={k}
          initial={{ opacity: 0, y: -y }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y, transition: EXIT }}
          transition={{ duration: 0.2, ease: EASE_OUT }}
          style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}
        >
          {children}
        </m.span>
      </AnimatePresence>
    </span>
  );
}

/**
 * Pops its content once whenever `value` changes (a count that went up or down).
 *
 * @param props.value - What to watch.
 * @param props.children - What pops.
 * @returns The span.
 */
export function Bump({ value, children }: { value: unknown; children: ReactNode }) {
  const [scope, run] = useAnimate();
  const prev = useRef(value);
  const reduced = useMotionReduced();
  useEffect(() => {
    if (prev.current !== value && !reduced && scope.current) run(scope.current, { scale: [1.35, 1] }, POP);
    prev.current = value;
  }, [value]);                                         // eslint-disable-line react-hooks/exhaustive-deps
  return <span ref={scope} style={{ display: 'inline-flex' }}>{children}</span>;
}

/**
 * Lets its height follow its content with the spring, so a block that grows
 * or shrinks (another form, a preview appearing) never jumps the page.
 *
 * @param props.children - The content.
 * @returns The wrapper.
 */
export function AutoHeight({ children }: { children: ReactNode }) {
  const calm = useCalm();
  const inner = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number | 'auto'>('auto');
  const [moving, setMoving] = useState(false);
  useLayoutEffect(() => {
    const el = inner.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => setHeight(el.offsetHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return (
    <m.div
      initial={false}
      animate={{ height }}
      transition={calm ? INSTANT : SPRING}
      onAnimationStart={() => setMoving(true)}
      onAnimationComplete={() => setMoving(false)}
      style={{ overflow: moving ? 'hidden' : 'visible', minWidth: 0 }}
    >
      <div ref={inner} style={{ display: 'flow-root' }}>{children}</div>
    </m.div>
  );
}

/**
 * Folds its content open when `show` turns true and shut when it turns false,
 * keeping the last content visible while it folds (alerts, extra fields).
 *
 * @param props.show - Whether the content is there.
 * @param props.children - The content.
 * @returns The folding block, or nothing.
 */
export function Fold({ show, children }: { show: boolean; children: ReactNode }) {
  const calm = useCalm();
  return (
    <AnimatePresence initial={false}>
      {show && (
        <m.div
          key="fold"
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: 'auto', opacity: 1 }}
          exit={{ height: 0, opacity: 0, transition: calm ? INSTANT : { duration: 0.22, ease: EASE_IN } }}
          transition={calm ? INSTANT : { height: SPRING, opacity: { duration: 0.2 } }}
          style={{ overflow: 'hidden' }}
        >
          {children}
        </m.div>
      )}
    </AnimatePresence>
  );
}

/**
 * Holds every CSS animation inside it until it scrolls into view, then plays
 * them once. Charts below the fold are seen drawing instead of already drawn.
 *
 * @param props.children - The chart.
 * @param props.sx - Extra styles.
 * @returns The wrapper.
 */
export function Seen({ children, sx }: { children: ReactNode; sx?: SxProps<Theme> }) {
  const ref = useRef<HTMLDivElement>(null);
  const seen = useInView(ref, { once: true, amount: 0.2 });
  return (
    <Box ref={ref} sx={[{ '& *': { animationPlayState: seen ? 'running' : 'paused' } }, ...(Array.isArray(sx) ? sx : [sx])]}>
      {children}
    </Box>
  );
}

/** A drawn tick for done states; it draws itself after `delay` seconds. */
export function DrawnTick({ size = 28, delay = 0.2, stroke = 3 }: { size?: number; delay?: number; stroke?: number }) {
  return (
    <Box component="svg" viewBox="0 0 26 26" aria-hidden sx={{ width: size, height: size, display: 'block' }}>
      <Box
        component="path" d="M6 13.5l5 5 9-11" pathLength={1}
        sx={{
          fill: 'none', stroke: 'currentColor', strokeWidth: stroke, strokeLinecap: 'round', strokeLinejoin: 'round',
          strokeDasharray: 1, animation: `${KF.draw} .42s ${EASE.out} ${delay}s both`,
        }}
      />
    </Box>
  );
}

/**
 * Snackbars slide up from the edge with the spring and drop away quickly.
 * Set as every Snackbar's transition in the theme.
 */
export const SlideUp = forwardRef(function SlideUp(props: TransitionProps & { children: React.ReactElement }, ref: React.Ref<unknown>) {
  return (
    <Slide
      {...props}
      ref={ref}
      direction="up"
      easing={{ enter: CSS_SPRING, exit: EASE.in }}
      timeout={{ enter: CSS_SPRING_MS, exit: DUR.exit }}
    />
  );
});

/**
 * A dialog sinks back the way it rose as it closes. Passed to every Dialog's
 * transition in the theme (its fade handles the opacity).
 *
 * @param node - The dialog's container.
 */
export function sinkDialog(node: HTMLElement) {
  if (prefersReduced()) return;
  const paper = node.querySelector('.MuiDialog-paper');
  paper?.animate([{ transform: 'none' }, { transform: 'translateY(8px) scale(.98)' }], { duration: DUR.exit, easing: EASE.in, fill: 'forwards' });
  // Held only while it closes: a dialog opened again straight away starts clean.
  setTimeout(() => paper?.getAnimations().forEach((a) => a.cancel()), DUR.exit + 120);
}
