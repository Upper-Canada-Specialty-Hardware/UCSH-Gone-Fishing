import { ReactNode } from 'react';
import { Box, Paper, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { Sparkline } from './manager/Charts';

/**
 * One number tile: a label, the number, a line under it, and an optional sparkline.
 * Shared by the manager and employee dashboards so their numbers read the same.
 *
 * @param props.label - What it counts.
 * @param props.value - The number, already formatted.
 * @param props.note - The line under it.
 * @param props.warn - Show the note in the warn colour.
 * @param props.spark - Sparkline points, oldest first.
 * @returns The tile.
 */
export function Kpi({ label, value, note, warn, spark }: { label: string; value: string; note: string; warn?: boolean; spark?: number[] }) {
  const theme = useTheme();
  return (
    <Paper sx={{ p: 2, display: 'grid', alignContent: 'start', gap: 0.25 }}>
      <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
      <Typography sx={{ fontFamily: theme.tokens.display, fontSize: 32, fontWeight: 600, lineHeight: 1.15 }}>{value}</Typography>
      <Typography variant="caption" sx={{ color: warn ? 'warning.main' : 'text.secondary', fontWeight: warn ? 700 : 400 }}>{note}</Typography>
      {spark && <Sparkline values={spark} />}
    </Paper>
  );
}

/**
 * A titled panel.
 *
 * @param props.title - The heading.
 * @param props.aside - Small text at the right of the heading.
 * @param props.children - The panel's content.
 * @returns The panel.
 */
export function Panel({ title, aside, children }: { title: string; aside?: string; children: ReactNode }) {
  return (
    <Paper sx={{ overflow: 'hidden' }}>
      <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, px: 2.25, pt: 1.75, pb: 1, flexWrap: 'wrap' }}>
        <Typography sx={{ fontWeight: 700 }}>{title}</Typography>
        {aside && <Typography variant="caption" color="text.secondary" sx={{ ml: 'auto' }}>{aside}</Typography>}
      </Box>
      <Box sx={{ px: 2.25, pb: 2.25 }}>{children}</Box>
    </Paper>
  );
}
