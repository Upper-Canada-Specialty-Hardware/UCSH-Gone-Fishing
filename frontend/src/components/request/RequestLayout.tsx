import { ReactNode } from 'react';
import { Box, ButtonBase, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { AnimatePresence, m } from 'motion/react';
import BeachAccessOutlinedIcon from '@mui/icons-material/BeachAccessOutlined';
import ScheduleOutlinedIcon from '@mui/icons-material/ScheduleOutlined';
import SavingsOutlinedIcon from '@mui/icons-material/SavingsOutlined';
import { APP_NAME, BrandMark, ColorModeButton } from '../TopBar';
import { REQUEST_TYPES } from '../RequestForm';
import type { RequestType } from '../../api/intake';
import { DrawnTick, EASE, EXIT, KF, SPRING, riseAt } from '../Motion';

/** Colour changes ease in, so switching the kind of request feels like a shift of light, not a flash. */
const FADE = 'background-color .5s ease, color .5s ease, border-color .5s ease';

/** One step in the side panel's list. */
export interface StepItem {
  label: string;
  state: 'done' | 'on' | 'todo';
}

/**
 * The coloured panel beside the form: the app's name, a line in the mood of
 * the request, and the steps with the current one lit. On phones it shrinks to
 * a band across the top and the steps are left out.
 *
 * @param props.words - Headline and the line under it; empty strings show nothing.
 * @param props.steps - The steps, in order.
 * @param props.brand - Show the app's name and the light/dark switch (off under the top bar, which has both).
 * @param props.glow - Where the accent glow sits for this mood: x and y offset in px, and scale.
 * @returns The panel.
 */
export function SidePanel({ words, steps, brand = true, glow = [0, 0, 1] }: { words: [string, string]; steps: StepItem[]; brand?: boolean; glow?: [number, number, number] }) {
  const theme = useTheme();
  const ink = theme.tokens.primaryInk;                 // text on the panel colour
  return (
    <Box
      component="aside"
      sx={{
        bgcolor: 'primary.main', color: ink, transition: FADE,
        px: { xs: 2, sm: 5, md: 'clamp(20px, 5vw, 64px)' }, py: { xs: 3, md: 6 },
        display: 'grid', alignContent: 'space-between', gap: { xs: 2, md: 4 },
        position: 'relative', overflow: 'hidden',
      }}
    >
      {/* A soft circle of the accent colour in the corner, for warmth. It drifts slowly
          at rest and shifts to a new place with each mood. */}
      <Box aria-hidden sx={{
        position: 'absolute', width: 420, height: 420, right: -160, bottom: -160, pointerEvents: 'none',
        animation: `${KF.drift} 9s ease-in-out infinite alternate`,
      }}>
        <Box sx={{
          width: '100%', height: '100%', borderRadius: '50%', bgcolor: theme.tokens.accent, opacity: 0.35, filter: 'blur(2px)',
          transform: `translate(${glow[0]}px, ${glow[1]}px) scale(${glow[2]})`,
          transition: `transform 1.2s ${EASE.out}, background-color .5s ease`,
        }} />
      </Box>
      <Box sx={{ position: 'relative', zIndex: 1 }}>
        {brand && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25 }}>
            <BrandMark bg={ink} fg={theme.palette.primary.main} />
            <Typography sx={{ fontWeight: 700, flex: 1 }}>{APP_NAME}</Typography>
            <Box sx={{ '& .MuiButton-root': { borderColor: 'currentColor', color: ink } }}><ColorModeButton /></Box>
          </Box>
        )}
        {/* The mood's words fade over with the colour instead of snapping. */}
        <AnimatePresence mode="wait" initial={false}>
          <m.div
            key={words.join('|')}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0, transition: { duration: 0.38, ease: [0.2, 0.8, 0.2, 1] } }}
            exit={{ opacity: 0, y: -6, transition: EXIT }}
          >
            {words[0] && (
              <Typography
                component="h1"
                sx={{
                  fontFamily: theme.tokens.display, fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.1,
                  fontSize: { xs: 26, md: 'clamp(30px, 4vw, 44px)' }, mt: { xs: 2, md: 2.25 }, mb: 1.25, maxWidth: '14ch',
                }}
              >
                {words[0]}
              </Typography>
            )}
            {words[1] && <Typography sx={{ opacity: 0.85, maxWidth: '42ch', fontSize: 15 }}>{words[1]}</Typography>}
          </m.div>
        </AnimatePresence>
      </Box>

      {/* The steps: done ones ticked, the current one bold. */}
      <Box component="ol" sx={{ listStyle: 'none', m: 0, p: 0, display: { xs: 'none', md: 'grid' }, gap: 1.75, position: 'relative', zIndex: 1 }}>
        {steps.map((s, i) => (
          <Box component="li" key={s.label} aria-current={s.state === 'on' ? 'step' : undefined}
            sx={{ display: 'flex', alignItems: 'center', gap: 1.5, opacity: s.state === 'todo' ? 0.6 : s.state === 'on' ? 1 : 0.9, fontWeight: s.state === 'on' ? 700 : 500 }}>
            <Box
              sx={{
                width: 28, height: 28, borderRadius: '50%', border: '2px solid currentColor', flex: 'none',
                display: 'grid', placeItems: 'center', fontSize: 12, fontWeight: 700, transition: FADE,
                ...(s.state !== 'todo' && { bgcolor: ink, color: 'primary.main', borderColor: ink }),
                // The step just reached pops lightly; a finished one draws its tick.
                animation: s.state === 'on' ? `${KF.nudge} .45s ${EASE.out}` : 'none',
              }}
            >
              {s.state === 'done' ? <DrawnTick size={16} delay={0.12} stroke={3.2} /> : i + 1}
            </Box>
            {s.label}
          </Box>
        ))}
      </Box>
    </Box>
  );
}

/** An icon for each of the three forms. */
const TYPE_ICONS: Record<RequestType, ReactNode> = {
  leave: <BeachAccessOutlinedIcon fontSize="small" />,
  overtime: <ScheduleOutlinedIcon fontSize="small" />,
  'carryover-payout': <SavingsOutlinedIcon fontSize="small" />,
};

/**
 * The three forms as cards with an icon and a one-line hint; the chosen card
 * takes the page colour.
 *
 * @param props.value - The chosen form.
 * @param props.onPick - Called with the form picked.
 * @returns The group of cards.
 */
export function TypeCards({ value, onPick }: { value: RequestType; onPick: (t: RequestType) => void }) {
  const theme = useTheme();
  return (
    <Box role="group" aria-label="What is the request for?"
      sx={{ display: 'grid', gap: 1.25, gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'repeat(3, minmax(0, 1fr))' } }}>
      {REQUEST_TYPES.map((t) => {
        const on = value === t.value;
        return (
          <ButtonBase
            key={t.value}
            aria-pressed={on}
            onClick={() => onPick(t.value)}
            sx={{
              textAlign: 'left', justifyContent: 'flex-start', alignItems: 'stretch', alignContent: 'start', justifyItems: 'start',
              display: 'grid', gap: 0.5, p: 1.5, borderRadius: '12px', transition: `${FADE}, transform .12s ease`,
              border: `1.5px solid ${on ? 'transparent' : theme.tokens.line2}`,
              bgcolor: 'background.paper',
              '& > :not(.type-card-hl)': { position: 'relative', zIndex: 1 },
              '&:active': { transform: 'scale(.98)' },
            }}
          >
            {/* One highlight that glides to the card picked. */}
            {on && (
              <m.span
                layoutId="type-card-hl"
                className="type-card-hl"
                transition={SPRING}
                style={{
                  position: 'absolute', inset: -1.5, borderRadius: 12, zIndex: 0, pointerEvents: 'none',
                  border: `1.5px solid ${theme.palette.primary.main}`, boxShadow: `0 0 0 1px ${theme.palette.primary.main} inset`,
                  background: theme.tokens.primarySoft, transition: 'background-color .5s ease, border-color .5s ease, box-shadow .5s ease',
                }}
              />
            )}
            <Box sx={{
              width: 30, height: 30, borderRadius: '8px', display: 'grid', placeItems: 'center', transition: FADE,
              bgcolor: on ? 'primary.main' : theme.tokens.chip, color: on ? theme.tokens.primaryInk : 'text.secondary',
              animation: on ? `${KF.nudge} .45s ${EASE.out}` : 'none',
            }}>
              {TYPE_ICONS[t.value]}
            </Box>
            <Typography sx={{ fontWeight: 700, fontSize: 14 }}>{t.label}</Typography>
            <Typography variant="caption" color="text.secondary" sx={{ lineHeight: 1.35 }}>{t.hint}</Typography>
          </ButtonBase>
        );
      })}
    </Box>
  );
}

/** One line of the "what happens next" list. */
export interface NextItem {
  title: string;
  note?: string;
  done?: boolean;
}

/**
 * What happens after sending, as a short vertical timeline: the first point
 * ticked (it just happened), the rest numbered and joined by a line.
 *
 * @param props.items - The points, in order.
 * @returns The list.
 */
export function NextSteps({ items }: { items: NextItem[] }) {
  const theme = useTheme();
  return (
    <Box component="ol" sx={{ listStyle: 'none', m: 0, p: 0, display: 'grid' }}>
      {items.map((x, i) => (
        <Box
          component="li"
          key={x.title}
          sx={{
            display: 'grid', gridTemplateColumns: '28px 1fr', gap: 1.5, pb: 2, position: 'relative', fontSize: 13.5,
            // The points fill in one by one after the tick has drawn.
            ...riseAt(i, { delay: 450, step: 120, dur: 320 }),
            // The line down to the next point draws down after its point; none under the last.
            '&::before': i < items.length - 1 ? {
              content: '""', position: 'absolute', left: 13, top: 28, bottom: 0, width: 2, bgcolor: 'divider',
              transformOrigin: 'top', animation: `${KF.growY} .3s ${EASE.out} ${560 + i * 120}ms both`,
            } : undefined,
          }}
        >
          <Box sx={{
            width: 28, height: 28, borderRadius: '50%', display: 'grid', placeItems: 'center', fontSize: 12, fontWeight: 700,
            bgcolor: x.done ? 'success.main' : theme.tokens.chip, color: x.done ? theme.palette.background.paper : 'text.primary',
          }}>
            {x.done ? '✓' : i + 1}
          </Box>
          <Box sx={{ pt: 0.5 }}>
            <Typography sx={{ fontWeight: 700, fontSize: 14 }}>{x.title}</Typography>
            {x.note && <Typography variant="body2" color="text.secondary">{x.note}</Typography>}
          </Box>
        </Box>
      ))}
    </Box>
  );
}
