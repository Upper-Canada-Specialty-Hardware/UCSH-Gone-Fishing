import { ReactNode } from 'react';
import { Box, ButtonBase } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { m } from 'motion/react';
import { SPRING } from './Motion';

/**
 * A row of pills where one is picked, drawn like MUI's outlined and filled
 * chips. The filled background is one shape that glides to the pill picked.
 *
 * @param props.options - Each pill: its key and label.
 * @param props.value - The key picked.
 * @param props.onChange - Called with the key clicked.
 * @param props.name - Unique per row on a page, so two rows never share a fill.
 * @param props.label - What the group is, for screen readers.
 * @returns The pills.
 */
export default function PillToggle<K extends string>({ options, value, onChange, name, label }: {
  options: { key: K; label: ReactNode }[]; value: K; onChange: (k: K) => void; name: string; label: string;
}) {
  const theme = useTheme();
  return (
    <Box role="group" aria-label={label} sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
      {options.map((o) => {
        const on = o.key === value;
        return (
          <ButtonBase
            key={o.key}
            aria-pressed={on}
            onClick={() => onChange(o.key)}
            sx={{
              height: 32, px: 1.5, borderRadius: 16, fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap',
              border: `1px solid ${on ? 'transparent' : theme.tokens.line2}`,
              color: on ? 'primary.contrastText' : 'text.primary',
              transition: 'color .25s ease, border-color .25s ease, background-color .2s ease, transform .12s ease',
              '&:hover': { bgcolor: on ? 'transparent' : theme.tokens.hover },
              '&:active': { transform: 'scale(.97)' },
              '&.Mui-focusVisible': { outline: `2px solid ${theme.palette.primary.main}`, outlineOffset: 2 },
            }}
          >
            {on && (
              <m.span
                layoutId={`${name}-pill-fill`}
                transition={SPRING}
                style={{ position: 'absolute', inset: -1, borderRadius: 16, background: theme.palette.primary.main, zIndex: 0 }}
              />
            )}
            <Box component="span" sx={{ position: 'relative', zIndex: 1, display: 'inline-flex', alignItems: 'center', gap: 0.5 }}>{o.label}</Box>
          </ButtonBase>
        );
      })}
    </Box>
  );
}
