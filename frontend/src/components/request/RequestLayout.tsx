import { ReactNode } from 'react';
import { Box, ButtonBase, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import BeachAccessOutlinedIcon from '@mui/icons-material/BeachAccessOutlined';
import ScheduleOutlinedIcon from '@mui/icons-material/ScheduleOutlined';
import SavingsOutlinedIcon from '@mui/icons-material/SavingsOutlined';
import { APP_NAME, BrandMark, ColorModeButton } from '../TopBar';
import { REQUEST_TYPES } from '../RequestForm';
import type { RequestType } from '../../api/intake';

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
 * @returns The panel.
 */
export function SidePanel({ words, steps, brand = true }: { words: [string, string]; steps: StepItem[]; brand?: boolean }) {
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
        // A soft circle of the accent colour in the corner, for warmth.
        '&::after': {
          content: '""', position: 'absolute', width: 420, height: 420, borderRadius: '50%',
          right: -160, bottom: -160, bgcolor: theme.tokens.accent, opacity: 0.35, filter: 'blur(2px)',
          transition: 'background-color .5s ease', pointerEvents: 'none',
        },
      }}
    >
      <Box sx={{ position: 'relative', zIndex: 1 }}>
        {brand && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25 }}>
            <BrandMark bg={ink} fg={theme.palette.primary.main} />
            <Typography sx={{ fontWeight: 700, flex: 1 }}>{APP_NAME}</Typography>
            <Box sx={{ '& .MuiButton-root': { borderColor: 'currentColor', color: ink } }}><ColorModeButton /></Box>
          </Box>
        )}
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
              }}
            >
              {s.state === 'done' ? '✓' : i + 1}
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
              display: 'grid', gap: 0.5, p: 1.5, borderRadius: '12px', transition: FADE,
              border: `1.5px solid ${on ? theme.palette.primary.main : theme.tokens.line2}`,
              bgcolor: on ? theme.tokens.primarySoft : 'background.paper',
              boxShadow: on ? `0 0 0 1px ${theme.palette.primary.main} inset` : 'none',
            }}
          >
            <Box sx={{
              width: 30, height: 30, borderRadius: '8px', display: 'grid', placeItems: 'center', transition: FADE,
              bgcolor: on ? 'primary.main' : theme.tokens.chip, color: on ? theme.tokens.primaryInk : 'text.secondary',
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
            // The line down to the next point; none under the last.
            '&::before': i < items.length - 1 ? {
              content: '""', position: 'absolute', left: 13, top: 28, bottom: 0, width: 2, bgcolor: 'divider',
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
