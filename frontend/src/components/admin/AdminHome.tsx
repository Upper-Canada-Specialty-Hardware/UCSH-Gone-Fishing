import { Box, ButtonBase, Paper, Typography } from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import { CountUp, EASE, KF, riseAt } from '../Motion';
import KindPill from '../KindPill';
import CarryoverWindowCard from './CarryoverWindowCard';
import { daysWaiting, requestKind, requestWhat, requestWhen, requestWho } from '../../utils/requestText';
import { greeting } from '../../utils/greeting';

interface Props {
  /** Every pending request (admin view, all teams). */
  pending: any[];
  /** Open held requests: people not on staff yet. */
  heldCount: number;
  /** Requests that could not be fully processed. */
  stuckCount: number;
  /** Staff records with a setup problem. */
  setupCount: number;
  /** Pending requests whose managers are all on vacation (#370). */
  vacationCount: number;
  /** Open a screen by key. */
  onGo: (key: string) => void;
}

/**
 * The admin home: what needs attention, each one click from its screen, then
 * the requests that have waited longest.
 *
 * @param props - See {@link Props}.
 * @returns The home screen.
 */
export default function AdminHome({ pending, heldCount, stuckCount, setupCount, vacationCount, onGo }: Props) {
  const theme = useTheme();
  // Oldest first: these are the ones a manager is sitting on.
  const oldest = [...pending].sort((a, b) => (daysWaiting(b) ?? 0) - (daysWaiting(a) ?? 0));
  const maxWait = oldest.length ? daysWaiting(oldest[0]) : null;

  // The four things HR checks, with a line saying why each matters right now.
  const tiles = [
    { key: 'pending', label: 'Waiting on a manager', n: pending.length, note: maxWait ? `oldest waiting ${maxWait} day${maxWait === 1 ? '' : 's'}` : 'nothing waiting' },
    { key: 'vacation', label: 'Managers on vacation', n: vacationCount, note: vacationCount ? 'every manager off, nobody can approve' : 'none stuck' },
    { key: 'held', label: 'New hires to add', n: heldCount, note: heldCount ? 'their requests wait until they are added' : 'nobody waiting' },
    { key: 'setup', label: 'Setup problems', n: setupCount, note: setupCount ? 'these would stop a request reaching a manager' : 'every record checks out' },
    { key: 'stuck', label: 'Stuck requests', n: stuckCount, note: stuckCount ? 'not fully processed' : 'none' },
  ];

  return (
    <Box sx={{ display: 'grid', gap: 3 }}>
      <Box>
        <Typography variant="h4">{greeting()}</Typography>
        <Typography color="text.secondary">
          {tiles.some((t) => t.n > 0) ? 'Here is what needs attention.' : 'Nothing needs attention right now.'}
        </Typography>
      </Box>

      <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(min(210px, 100%), 1fr))' }}>
        {tiles.map((t, i) => (
          <ButtonBase
            key={t.key}
            onClick={() => onGo(t.key)}
            sx={{
              display: 'grid', gap: 0.5, justifyContent: 'stretch', justifyItems: 'start', alignContent: 'start', textAlign: 'left', p: 2.25,
              borderRadius: `${theme.shape.borderRadius}px`, border: 1, borderColor: 'divider',
              bgcolor: 'background.paper', overflow: 'hidden',
              transition: 'border-color .2s ease, transform .12s ease',
              '&:hover': { borderColor: theme.tokens.line2 },
              '&:active': { transform: 'scale(.98)' },
              ...riseAt(i, { step: 50 }),
              // A tile with work in it gets a terracotta edge so it is seen first; the edge draws down.
              '&::before': t.n > 0 ? {
                content: '""', position: 'absolute', left: 0, top: 0, bottom: 0, width: 4, bgcolor: theme.tokens.accent,
                transformOrigin: 'top', animation: `${KF.growY} .4s ${EASE.out} ${250 + i * 60}ms both`,
              } : undefined,
            }}
          >
            <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>{t.label}</Typography>
            <Typography sx={{ fontFamily: theme.tokens.display, fontSize: 34, fontWeight: 600, lineHeight: 1.1 }}><CountUp from={0} value={t.n} duration={0.7} /></Typography>
            <Typography variant="caption" sx={{ color: t.n > 0 ? 'text.primary' : 'text.secondary' }}>{t.note}</Typography>
          </ButtonBase>
        ))}
      </Box>

      <Paper>
        <Box sx={{ display: 'flex', alignItems: 'center', px: 2.5, py: 1.75, borderBottom: 1, borderColor: 'divider' }}>
          <Typography sx={{ fontWeight: 700 }}>Waiting longest</Typography>
          <ButtonBase onClick={() => onGo('pending')} sx={{ ml: 'auto', color: 'primary.main', fontWeight: 600, fontSize: 13 }}>
            All pending
          </ButtonBase>
        </Box>
        {oldest.length === 0 ? (
          <Typography color="text.secondary" sx={{ p: 2.5 }}>No requests are waiting.</Typography>
        ) : (
          oldest.slice(0, 6).map((r, i) => {
            const d = daysWaiting(r);
            return (
              <ButtonBase
                key={`${r.request_type}-${r.id}`}
                onClick={() => onGo('pending')}
                sx={{
                  width: '100%', display: 'grid', justifyContent: 'stretch',
                  // Narrow screens drop the date column; the row stays one line.
                  gridTemplateColumns: { xs: 'minmax(0,1fr) auto auto', sm: 'minmax(0,1.2fr) auto minmax(0,1fr) auto' },
                  gap: 2, alignItems: 'center', textAlign: 'left', px: 2.5, py: 1.25,
                  borderTop: 1, borderColor: 'divider', '&:hover': { bgcolor: theme.tokens.hover },
                  // Oldest first, in time as well as place.
                  ...riseAt(i, { delay: 150, step: 50, dur: 320 }),
                }}
              >
                <Typography noWrap sx={{ fontWeight: 600 }}>{requestWho(r)}</Typography>
                <KindPill kind={requestKind(r)} label={requestWhat(r)} />
                <Typography noWrap variant="body2" color="text.secondary" sx={{ display: { xs: 'none', sm: 'block' } }}>{requestWhen(r)}</Typography>
                <Typography
                  variant="caption"
                  sx={{
                    fontWeight: 700, px: 1, borderRadius: 999,
                    // A week or more is late: warn colour.
                    color: d !== null && d > 7 ? 'warning.main' : 'text.secondary',
                    bgcolor: d !== null && d > 7 ? alpha(theme.palette.warning.main, 0.12) : 'transparent',
                  }}
                >
                  {d === null ? '' : d === 0 ? 'today' : `${d} d`}
                </Typography>
              </ButtonBase>
            );
          })
        )}
      </Paper>

      {/* Opened for the carry-over season, closed the rest of the year. */}
      <CarryoverWindowCard />
    </Box>
  );
}
