import { Box, Button, Chip, Paper, Typography } from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import KindPill from '../KindPill';
import TakenFrom from '../TakenFrom';
import { canDecide, stateDisplay } from '../../requestState';
import { parseDay } from '../../utils/dates';
import { ColorOf } from '../../utils/personColor';
import { Clash } from './teamStats';
import { askedAgo, daysWaiting, requestAmount, requestKind, requestNote, requestWhat, requestWhen, requestWho } from '../../utils/requestText';
import { BalanceMeter } from './Charts';
import { Swap, riseAt } from '../Motion';
import { belowZeroSentence, isHourly } from '../../utils/employment';

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
  /** While busy, which way the decision goes: true approving, false rejecting, null idle (#455). */
  busyYes?: boolean | null;
  onApprove: () => void;
  onReject: () => void;
  /** Its place in the queue, so cards arrive oldest first. */
  index?: number;
  /** Set once decided: true approved, false rejected. The card shows it before it leaves. */
  decided?: boolean;
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
export default function DecisionCard({ item, clashes, processingEnabled, busy, busyYes = null, onApprove, onReject, colorOf, index = 0, decided }: Props) {
  const theme = useTheme();
  const who = requestWho(item);
  const note = requestNote(item);
  const waited = daysWaiting(item);
  const cur = item.current_balances, next = item.projected_balances;
  const hourlyStaff = isHourly(cur);                                             // hourly: Sick only, no vacation balance (#300)
  const belowZero = item.below_zero;                                            // set when approving drops the floor below zero (#300)
  const dated = item.request_type !== 'carryover-payout' && item.StartDate;     // carry-over has no dates
  const sameDay = !item.EndDate || item.EndDate.slice(0, 10) === item.StartDate?.slice(0, 10);
  const decidable = canDecide(item.state);                                      // false while getting ready / needs attention (#455)
  // Surface the state chip only while a card is still being prepared
  // (getting_ready / needs_attention): that is the whole reason its buttons are
  // off, so the manager sees why (#455). A waiting_on_approval row needs no chip,
  // a decided row must not show a second "Approved"-style chip on top of its
  // outcome, and the SharePoint path has no state.
  const notYetReady = item.state === 'getting_ready' || item.state === 'needs_attention';
  const stateChip = notYetReady ? stateDisplay(item.state) : null;
  // The tooltip on the disabled buttons when the block is the state, not processing.
  const notReadyHint = stateChip ? `${stateChip.label}: this request cannot be decided yet` : undefined;

  return (
    <Paper
      component="article"
      sx={{
        p: 2.25, display: 'grid', gap: 1.75, position: 'relative', overflow: 'hidden',
        // Cards rise in one after another, oldest first.
        ...riseAt(index, { step: 60, max: 4, dur: 450 }),
        // Once decided, a wash in the outcome's colour before the card leaves.
        '&::after': {
          content: '""', position: 'absolute', inset: 0, pointerEvents: 'none', transition: 'opacity .16s ease',
          bgcolor: decided === false ? theme.tokens.errSoft : theme.tokens.okSoft, opacity: decided === undefined ? 0 : 0.85,
        },
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
        {/* Why this card is not decidable yet (getting ready / needs attention), #455. */}
        {stateChip && (
          <Chip size="small" color={stateChip.color} variant="outlined" label={stateChip.label} />
        )}
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
          {/* Show every pot that moves; Vacation is always shown too, except for hourly who have no vacation balance (#300). */}
          {POTS.filter((p) => (cur[p.key] ?? 0) !== (next[p.key] ?? 0) || (p.key === 'vacation_balance' && !hourlyStaff)).map((p) => (
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

      {/* Which balances approving would draw from, as projected chips under the bars. */}
      {item.taken_from?.state === 'projected' && (
        <Box sx={{ display: 'flex', gap: 1, alignItems: 'baseline', flexWrap: 'wrap' }}>
          <Typography variant="body2" color="text.secondary">Taken from</Typography>
          <TakenFrom value={item.taken_from} compact />
        </Box>
      )}

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

      {/* Approving would take the floor below zero: a red banner, never a block (#300).
          The manager may still approve; it is their call. */}
      {belowZero && (
        <Box
          role="alert"
          sx={{
            display: 'flex', flexDirection: 'column', gap: 0.25, px: 1.5, py: 1, borderRadius: '10px',
            bgcolor: theme.tokens.errSoft, border: `1px solid ${alpha(theme.palette.error.main, 0.5)}`, color: 'error.main',
          }}
        >
          <Typography variant="body2" sx={{ fontWeight: 600 }}>{belowZeroSentence(belowZero)}</Typography>
          {/* The projected floor value, in red, so the number is right there. */}
          <Typography variant="caption" sx={{ fontVariantNumeric: 'tabular-nums' }}>
            {belowZero.pot === 'sick' ? 'Sick' : 'Vacation'}: {belowZero.before} → {belowZero.after}
          </Typography>
        </Box>
      )}

      {/* The pressed button confirms in place ("Approved" with a tick) before the card leaves.
          A card that is not decidable yet (getting ready / needs attention) has both buttons
          off, with the state as the reason in the tooltip (#455). */}
      <Box sx={{ display: 'flex', gap: 1, position: 'relative', zIndex: 1 }}>
        <Button variant="contained" color="success" disabled={!processingEnabled || busy || decided !== undefined || !decidable} onClick={onApprove}
          title={!processingEnabled ? 'Processing is currently disabled' : notReadyHint}>
          {/* While this card's approval runs, the button says "Approving..."; a reject in flight leaves it on "Approve". */}
          <Swap k={busyYes === true ? 'busy' : decided === true ? 'done' : 'ask'}>
            {busyYes === true ? 'Approving...' : decided === true ? '✓ Approved' : 'Approve'}
          </Swap>
        </Button>
        <Button variant="outlined" color="error" disabled={!processingEnabled || busy || decided !== undefined || !decidable} onClick={onReject}
          title={!processingEnabled ? 'Processing is currently disabled' : notReadyHint}>
          {/* While this card's rejection runs, the button says "Rejecting...". */}
          <Swap k={busyYes === false ? 'busy' : decided === false ? 'done' : 'ask'}>
            {busyYes === false ? 'Rejecting...' : decided === false ? '✓ Rejected' : 'Reject'}
          </Swap>
        </Button>
      </Box>
    </Paper>
  );
}
