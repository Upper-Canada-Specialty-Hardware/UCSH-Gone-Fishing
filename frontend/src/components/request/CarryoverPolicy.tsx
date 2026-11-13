import { Box, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { longDate } from '../../utils/carryoverWindow';

/**
 * HR's carry-over and payout policy, shown above the form before it is filled in
 * (#429). It states the rules staff need before they ask: check your balance
 * first, pending requests are not counted, payout is capped at 5 days, carried
 * days expire on March 31, and carry-over and payout need separate requests. The
 * open and deadline dates come from the window HR set (never typed here); they
 * are left out when HR has not set a window.
 *
 * @param props.window - The carry-over/payout window (ISO dates), or null when
 *   HR has not set one.
 * @returns The policy panel.
 */
export default function CarryoverPolicy({ window }: { window: { start: string; end: string } | null }) {
  const theme = useTheme();
  const t = theme.tokens;
  // Payout only now: carry-over happens on its own at year end (#554). Kept short;
  // the dates line is added only when HR has set a window.
  const points: string[] = [
    'Payout is capped: your total paid-out days for the year cannot go above 5.',
    'Days on requests that are still pending are not counted yet.',
  ];
  if (window) {
    // The window is the time HR accepts these requests; its end is the deadline to send.
    points.unshift(`Open from ${longDate(window.start)} until ${longDate(window.end)}.`);
  }
  // No FAQ any more: the two lines above are the whole policy.
  const faq: [string, string][] = [];
  return (
    <Box
      sx={{
        // A quiet panel in the page colour, so it reads as guidance, not a warning.
        border: `1px solid ${t.line2}`, borderRadius: '12px', bgcolor: t.primarySoft,
        px: 2, py: 1.75, display: 'grid', gap: 1.25, minWidth: 0,
      }}
    >
      <Typography sx={{ fontWeight: 700, fontSize: 14 }}>Before you send a payout request</Typography>
      {/* The rules as a tight list; everything wraps, so a phone never scrolls sideways. */}
      <Box component="ul" sx={{ m: 0, pl: 2.5, display: 'grid', gap: 0.5 }}>
        {points.map((p) => (
          <Typography key={p} component="li" variant="body2" color="text.secondary" sx={{ overflowWrap: 'anywhere' }}>
            {p}
          </Typography>
        ))}
      </Box>
      {/* A short FAQ under a faint divider, kept plain so HR can reword it. */}
      <Box sx={{ borderTop: `1px solid ${t.line}`, pt: 1, display: faq.length ? 'grid' : 'none', gap: 1 }}>
        {faq.map(([q, a]) => (
          <Box key={q} sx={{ display: 'grid', gap: 0.25, minWidth: 0 }}>
            <Typography variant="body2" sx={{ fontWeight: 600, overflowWrap: 'anywhere' }}>{q}</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ overflowWrap: 'anywhere' }}>{a}</Typography>
          </Box>
        ))}
      </Box>
    </Box>
  );
}
