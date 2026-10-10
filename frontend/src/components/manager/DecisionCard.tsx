import { Box, Button, Paper, Typography } from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import KindPill from '../KindPill';
import { parseDay } from '../../utils/dates';
import { ColorOf } from '../../utils/personColor';
import { Clash } from './teamStats';
import { askedAgo, daysWaiting, requestAmount, requestKind, requestNote, requestWhat, requestWhen, requestWho } from '../../utils/requestText';
import { BalanceMeter } from './Charts';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** The balances shown on a card, with the entitlement that sets each bar's length. */
const POTS: { key: string; label: string; of?: string }[] = [
  { key: 'vacation_balance', label: 'Vacation', of: 'vacation_entitlement' },
  { key: 'sick_balance', label: 'Sick', of: 'sick_entitlement' },
  { key: 'overtime', label: 'Make-Up' },
  { key: 'carryover', label: 'Carry Over' },
];

interface Props {
  /** A /team/pending item. */
  item: any;
  /** Others away during these dates, from clashesFor; each has a unique key. */
  clashes: Clash[];
  processingEnabled: boolean;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
  /** Each person's colour, for the avatar. */
  colorOf: ColorOf;
}

/**
 * A calendar-page date: small month over a big day number.
 *
 * @param props.iso - The date.
 * @returns The box, or nothing without a date.
 */
function DateBox({ iso }: { iso?: string }) {
  const theme = useTheme();
  const d = parseDay(iso);
  if (!d) return null;
  return (
    <Box sx={{ width: 50, textAlign: 'center', borderRadius: '10px', border: 1, borderColor: 'divider', overflow: 'hidden', flex: 'none' }}>
      <Box sx={{ fontSize: 11, fontWeight: 700, color: theme.palette.mode === 'dark' ? theme.palette.background.default : '#fff', bgcolor: theme.tokens.accent, py: 0.25 }}>{MONTHS[d.getMonth()]}</Box>
      <Box sx={{ fontFamily: theme.tokens.display, fontSize: 22, fontWeight: 600, lineHeight: 1.4 }}>{d.getDate()}</Box>
    </Box>
  );
}

/**
 * One waiting request as a card a manager can decide from: who and what,
 * the dates, the person's own words, a bar per balance showing what approval
 * uses, anyone else away at the same time, and the two buttons.
 *
 * @param props - See {@link Props}.
 * @returns The card.
 */
export default function DecisionCard({ item, clashes, processingEnabled, busy, onApprove, onReject, colorOf }: Props) {
  const theme = useTheme();
  const who = requestWho(item);
  const note = requestNote(item);
  const waited = daysWaiting(item);
  const cur = item.current_balances, next = item.projected_balances;
  const dated = item.request_type !== 'carryover-payout' && item.StartDate;     // carry-over has no dates
  const sameDay = !item.EndDate || item.EndDate.slice(0, 10) === item.StartDate?.slice(0, 10);

  return (
    <Paper
      component="article"
      sx={{
        p: 2.25, display: 'grid', gap: 1.75,
        animation: 'cardin .45s both ease-out', '@keyframes cardin': { from: { opacity: 0, transform: 'translateY(8px)' } },
        '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
      }}
    >
      {/* Who, when they asked, what kind, and a warning once it has waited over a week. */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
        <Box sx={{ width: 40, height: 40, borderRadius: 999, bgcolor: colorOf(who), color: theme.palette.mode === 'dark' ? theme.palette.background.default : '#fff', display: 'grid', placeItems: 'center', fontWeight: 700, flex: 'none' }}>
          {who.split(' ').map((w) => w[0]).slice(0, 2).join('')}
        </Box>
        <Box sx={{ flex: 1, minWidth: 140 }}>
          <Typography sx={{ fontWeight: 700 }}>{who}</Typography>
          <Typography variant="body2" color="text.secondary">{askedAgo(item)}</Typography>
        </Box>
        <KindPill kind={requestKind(item)} label={requestWhat(item)} />
        {waited !== null && waited > 7 && (
          <Box component="span" sx={{ fontSize: 12, fontWeight: 700, px: 1, py: 0.25, borderRadius: 999, color: 'warning.main', bgcolor: alpha(theme.palette.warning.main, 0.12) }}>
            waiting {waited} days
          </Box>
        )}
      </Box>

      {/* The dates as calendar pages, the amount, and the person's note. */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
        {dated ? (
          <>
            <DateBox iso={item.StartDate} />
            {!sameDay && <><Typography color="text.secondary">→</Typography><DateBox iso={item.EndDate} /></>}
          </>
        ) : null}
        <Box sx={{ minWidth: 0 }}>
          <Typography sx={{ fontWeight: 700 }}>{dated ? requestAmount(item) : requestWhen(item)}</Typography>
          {note && <Typography variant="body2" color="text.secondary" sx={{ fontStyle: 'italic' }}>"{note}"</Typography>}
        </Box>
      </Box>

      {/* What approving does to each balance; hourly staff and no-cost leave get the backend's sentence instead. */}
      {cur && (item.balance_unchanged ? (
        <Typography variant="body2" color="text.secondary" sx={{ fontStyle: 'italic' }}>{item.balance_unchanged}</Typography>
      ) : next && (
        <Box sx={{ display: 'grid', gap: 1 }}>
          {POTS.filter((p) => (cur[p.key] ?? 0) !== (next[p.key] ?? 0) || p.key === 'vacation_balance').map((p) => (
            <Box key={p.key} sx={{ display: 'grid', gridTemplateColumns: '86px minmax(0, 1fr) auto', gap: 1.5, alignItems: 'center' }}>
              <Typography variant="body2" color="text.secondary">{p.label}</Typography>
              <BalanceMeter cur={cur[p.key] ?? 0} next={next[p.key] ?? 0} max={p.of ? cur[p.of] ?? 0 : 5} />
              <Typography variant="body2" sx={{ fontWeight: 700, fontVariantNumeric: 'tabular-nums', color: (next[p.key] ?? 0) < 0 ? 'error.main' : 'text.primary' }}>
                {cur[p.key] ?? 0} → {next[p.key] ?? 0}
              </Typography>
            </Box>
          ))}
        </Box>
      ))}

      {/* Others away at the same time. */}
      {clashes.length > 0 && (
        <Box sx={{ display: 'flex', gap: 0.75, flexWrap: 'wrap' }}>
          {clashes.map((c) => (
            // Keyed by the clashing absence's own id, so two teammates sharing a first name never collide.
            <Box key={c.key} component="span" sx={{ fontSize: 12, fontWeight: 600, px: 1, py: 0.25, borderRadius: 999, color: theme.tokens.accent, bgcolor: alpha(theme.tokens.accent, 0.12) }}>
              {c.text}
            </Box>
          ))}
        </Box>
      )}

      <Box sx={{ display: 'flex', gap: 1 }}>
        <Button variant="contained" color="success" disabled={!processingEnabled || busy} onClick={onApprove}
          title={!processingEnabled ? 'Processing is currently disabled' : undefined}>
          Approve
        </Button>
        <Button variant="outlined" color="error" disabled={!processingEnabled || busy} onClick={onReject}
          title={!processingEnabled ? 'Processing is currently disabled' : undefined}>
          Reject
        </Button>
      </Box>
    </Paper>
  );
}
