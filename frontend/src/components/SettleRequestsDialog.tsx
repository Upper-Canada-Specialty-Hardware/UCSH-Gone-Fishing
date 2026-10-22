import { useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, Radio, RadioGroup, TextField, Typography,
} from '@mui/material';
import { useTheme } from '@mui/material/styles';
import TakenFrom from './TakenFrom';
import { formatDay, formatDayRange, formatInstantDay } from '../utils/dates';
import { Pot, TakenFromValue, fmtDays } from '../utils/takenFrom';
import { SettleOutcome, SettlePreview, getSettlePreview, settleRequest } from '../api/requestsAdmin';

/** One flagged request, as the setup check lists it (approved, never moved a balance). */
export interface UnmovedRequest {
  kind: string;                      // 'leave' | 'overtime' | 'carryover_payout'
  item_id: number;
  leave_type: string | null;
  decided: string | null;            // ISO date
  start_date: string | null;
  end_date: string | null;
  days: number | null;
  hours: number | null;
  decided_by: string | null;
}

interface Props {
  /** Whose requests these are, for the title. */
  personName: string;
  /** The flagged requests to walk through, or null when the dialog is closed. */
  requests: UnmovedRequest[] | null;
  /**
   * Close the dialog.
   *
   * @param settled - The requests saved in this run (empty when none were), so
   *   the caller can drop them at once, say what happened and refresh.
   */
  onClose: (settled: UnmovedRequest[]) => void;
}

/** One finished step, for the review at the end. */
interface DoneStep {
  req: UnmovedRequest;
  outcome: string;                   // the plain sentence of what was recorded
  reason: string;
}

/** The pots a leave's days can have come from by hand, in the spending order. */
const HAND_POTS: Pot[] = ['make_up', 'carry_over', 'vacation', 'sick'];
/** Pot names as the dialog shows them. */
const POT_LABEL: Record<string, string> = {
  sick: 'Sick', make_up: 'Make-Up', carry_over: 'Carry-over', vacation: 'Vacation', payout: 'Payout',
  vacation_taken: 'Vacation taken this year',
};
/** The stored kind to the url's request type. */
const URL_TYPE: Record<string, string> = { leave: 'leave', overtime: 'overtime', carryover_payout: 'carryover-payout' };

/**
 * The request's kind in words: its leave type, or make-up / carry-over / payout.
 *
 * @param r - The flagged request.
 * @param option - The carry-over/payout choice from the preview, when known.
 * @returns A short label.
 */
export function kindLabel(r: UnmovedRequest, option?: string | null): string {
  if (r.leave_type) return r.leave_type;                        // leave: its own type
  if (r.kind === 'overtime') return 'Make-up';                  // overtime banks make-up
  return option === 'payout' ? 'Payout' : 'Carry-over';         // carry-over/payout by its option
}

/**
 * A balance with its sign, trimmed: "-1.5", "6", "0.125".
 *
 * @param n - The value.
 * @returns The text.
 */
function signed(n: number): string {
  return `${n < 0 ? '-' : ''}${fmtDays(n)}`;                     // fmtDays drops the sign; put it back
}

/**
 * The request's amount: days, or hours for make-up.
 *
 * @param r - The flagged request.
 * @returns e.g. "2 days", "4 hours".
 */
function amountLabel(r: UnmovedRequest): string {
  if (r.kind === 'overtime') return `${fmtDays(r.hours || 0)} hours`;   // make-up is entered in hours
  const d = r.days || 0;
  return `${fmtDays(d)} ${d === 1 ? 'day' : 'days'}`;
}

/**
 * Settle requests (#400): walks an admin through one person's approved requests
 * that never moved a balance, one at a time. For each, the admin picks what
 * happened to the days (take them off now, already taken off by hand, or no
 * balance needed), gives a reason, and saves; Skip leaves it flagged. A review
 * at the end lists what was recorded. Each save is its own server call, so a
 * request saved before a later failure stays saved.
 *
 * @param props - See {@link Props}.
 * @returns The dialog.
 */
export default function SettleRequestsDialog({ personName, requests, onClose }: Props) {
  const theme = useTheme();
  const list = requests || [];
  const open = requests !== null;
  const [idx, setIdx] = useState(0);                            // the step; list.length = the review
  const [preview, setPreview] = useState<SettlePreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [choice, setChoice] = useState<SettleOutcome>('now');
  const [reason, setReason] = useState('');
  const [hand, setHand] = useState<Record<string, string>>({}); // typed text per pot, so "1." can be typed
  const [done, setDone] = useState<DoneStep[]>([]);

  const onReview = open && idx >= list.length;                  // past the last request
  const cur = onReview ? null : list[idx];

  // A fresh open starts at the first request with nothing done.
  useEffect(() => {
    if (open) { setIdx(0); setDone([]); }
  }, [open]);

  // Each step loads what "take it off now" would do, and resets the choices.
  useEffect(() => {
    if (!open || !cur) return;
    let live = true;                                            // ignore a late answer after a step change
    setPreview(null); setError(''); setReason(''); setLoading(true);
    getSettlePreview(URL_TYPE[cur.kind] || cur.kind, cur.item_id)
      .then((res) => {
        if (!live) return;
        const p = res.data;
        setPreview(p);
        // Prefill the by-hand amounts with what approval would have taken.
        const pre: Record<string, string> = {};
        p.plan.moves.forEach((m) => { if (m.change < 0) pre[m.pot] = fmtDays(m.change); });
        setHand(pre);
        // Start on "take it off now"; a request that costs nothing, or cannot be
        // taken now, starts on "no balance needed" (the one choice always open).
        setChoice(p.plan.outcome === 'balance_applied' || p.plan.outcome === 'counted' ? 'now' : 'none');
      })
      .catch((e) => {
        if (!live) return;
        const detail = e?.response?.data?.detail;
        setError(typeof detail === 'string' ? detail : 'This request could not be loaded. Skip it or try again.');
      })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [open, idx]);                                              // cur follows idx; re-run per step only

  // The by-hand amounts as numbers, and whether they add up to the request.
  const handNums = useMemo(() => {
    const out: Record<string, number> = {};
    Object.entries(hand).forEach(([pot, text]) => { const n = Number(text); if (n > 0) out[pot] = n; });
    return out;
  }, [hand]);
  const handSum = Object.values(handNums).reduce((t, n) => t + n, 0);
  const days = cur?.days || 0;
  const handOk = Math.abs(handSum - days) < 0.001 && Object.values(hand).every((t) => t === '' || Number(t) >= 0);

  // The chips each choice would record, drawn with the shared "Taken from" display.
  const nowChips: TakenFromValue = {
    state: 'projected',
    pieces: (preview?.plan.moves || []).filter((m) => m.pot !== 'vacation_taken')
      .map((m) => ({ pot: m.pot as Pot, days: m.change, year: null })),
  };
  const handChips: TakenFromValue = {
    state: 'recorded',
    pieces: preview?.hand_editable
      ? Object.entries(handNums).map(([pot, n]) => ({ pot: pot as Pot, days: -n, year: null }))
      : nowChips.pieces,                                         // other kinds record the plan as it is
  };

  const plan = preview?.plan.outcome;
  const canNow = plan === 'balance_applied' || plan === 'counted';
  const canHand = plan === 'balance_applied';                    // counted is the ledger itself; nothing to do by hand
  const canSave = !!preview && !saving && reason.trim().length > 0
    && (choice !== 'hand' || !preview.hand_editable || handOk)
    && (choice !== 'now' || canNow) && (choice !== 'hand' || canHand);

  /** Move to the next step with a clean slate. */
  const next = () => setIdx((i) => i + 1);

  /** Save this step, record it for the review, and go on. */
  const save = async () => {
    if (!cur || !canSave) return;
    setSaving(true); setError('');
    try {
      const body: { outcome: SettleOutcome; reason: string; pieces?: Record<string, number> } = { outcome: choice, reason: reason.trim() };
      if (choice === 'hand' && preview?.hand_editable) body.pieces = handNums;   // which pots, for a leave
      const res = await settleRequest(URL_TYPE[cur.kind] || cur.kind, cur.item_id, body);
      const moves = res.data.moves || [];
      const text = res.data.outcome === 'none'
        ? 'No balance needed. Nothing moved.'
        : res.data.outcome === 'hand'
          ? `Already taken off by hand: ${moves.map((m) => `${POT_LABEL[m.pot] || m.pot} ${fmtDays(m.change)}`).join(', ')}. Balances unchanged.`
          : `Taken off now: ${moves.map((m) => `${POT_LABEL[m.pot] || m.pot} ${signed(m.before)} → ${signed(m.after)}`).join(', ')}.`;
      setDone((d) => [...d, { req: cur, outcome: text, reason: reason.trim() }]);
      next();
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'This could not be saved. Try again.');
    } finally {
      setSaving(false);
    }
  };

  const optionSx = (key: SettleOutcome) => ({
    m: 0, px: 1.5, py: 1, borderRadius: 2, alignItems: 'flex-start',
    border: `${choice === key ? 2 : 1}px solid ${choice === key ? theme.palette.primary.main : theme.tokens.line}`,
    bgcolor: choice === key ? theme.tokens.primarySoft : 'transparent',
  });

  return (
    <Dialog open={open} onClose={() => onClose(done.map((d) => d.req))} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ pb: 1 }}>
        <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 2, alignItems: 'baseline' }}>
          <span>Settle {personName}'s requests</span>
          <Typography variant="body2" color="text.secondary">
            {onReview ? 'Review' : `Request ${idx + 1} of ${list.length}`}
          </Typography>
        </Box>
        {/* One segment per request plus the review: done, current, still to come. */}
        <Box sx={{ display: 'flex', gap: 0.75, mt: 1.25 }} aria-hidden>
          {[...list, null].map((_, i) => (
            <Box key={i} sx={{
              height: 6, flex: 1, borderRadius: 99,
              bgcolor: i < idx ? 'primary.main' : i === idx ? theme.tokens.accent : theme.tokens.line,
            }} />
          ))}
        </Box>
      </DialogTitle>

      <DialogContent dividers sx={{ display: 'grid', gap: 2 }}>
        {cur && (
          <>
            {/* The request being settled. */}
            <Box sx={{ bgcolor: theme.tokens.chip, borderRadius: 2, p: 1.5, display: 'grid', gap: 0.5 }}>
              <Typography sx={{ fontWeight: 700 }}>
                #{cur.item_id} {kindLabel(cur, preview?.request.request_option)}
                {cur.start_date ? `, ${formatDayRange(cur.start_date, cur.end_date)}` : ''}, {amountLabel(cur)}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                Approved {cur.decided ? formatDay(cur.decided) : (preview?.request.decided_at ? formatInstantDay(preview.request.decided_at) : '')}
                {cur.decided_by ? ` by ${cur.decided_by}` : ''}. No balance moved.
              </Typography>
            </Box>

            {loading && <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}><CircularProgress size={24} /></Box>}
            {error && <Alert severity="error">{error}</Alert>}

            {preview && (
              <>
                {/* The person's pots now. */}
                <Box>
                  <Typography variant="caption" sx={{ fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: 'text.secondary' }}>
                    Balances now
                  </Typography>
                  <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mt: 0.5 }}>
                    {['make_up', 'carry_over', 'vacation', 'sick', 'payout'].map((pot) => (
                      <Box key={pot} sx={{ px: 1.25, py: 0.5, border: `1px solid ${theme.tokens.line}`, borderRadius: 1.5, fontSize: 13.5 }}>
                        {POT_LABEL[pot]} <b>{signed(preview.pots[pot] || 0)}</b>
                      </Box>
                    ))}
                  </Box>
                </Box>

                {/* What happened to these days? */}
                <RadioGroup value={choice} onChange={(e) => setChoice(e.target.value as SettleOutcome)} sx={{ gap: 1 }}>
                  <FormControlLabel
                    value="now" disabled={!canNow} control={<Radio size="small" />} sx={optionSx('now')}
                    label={<Box><b>Take it off now</b><Typography variant="body2" color="text.secondary">
                      {plan === 'refused_negative'
                        ? 'Not possible: it would put vacation below zero.'
                        : 'Moves the balances the way approval should have, using the normal order.'}
                    </Typography></Box>}
                  />
                  <FormControlLabel
                    value="hand" disabled={!canHand} control={<Radio size="small" />} sx={optionSx('hand')}
                    label={<Box><b>Already taken off by hand</b><Typography variant="body2" color="text.secondary">
                      Someone already lowered the balance. Record where the days came from; nothing moves.
                    </Typography></Box>}
                  />
                  <FormControlLabel
                    value="none" control={<Radio size="small" />} sx={optionSx('none')}
                    label={<Box><b>No balance needed</b><Typography variant="body2" color="text.secondary">
                      {plan === 'no_balance_used'
                        ? (preview.plan.note || 'This request uses no balance.')
                        : 'This request should never cost days, like a test request or a day the office was closed.'}
                    </Typography></Box>}
                  />
                </RadioGroup>

                {/* The effect of the choice. */}
                {choice === 'now' && canNow && (
                  <Box sx={{ border: `1px solid ${theme.tokens.line}`, borderRadius: 2, p: 1.5, display: 'grid', gap: 0.75 }}>
                    <Typography variant="body2" sx={{ fontWeight: 700 }}>Saving moves these balances</Typography>
                    {preview.plan.moves.map((m) => (
                      <Typography key={m.pot} variant="body2">
                        {POT_LABEL[m.pot] || m.pot}: {signed(m.before)} → <b>{signed(m.after)}</b>
                      </Typography>
                    ))}
                    {nowChips.pieces.length > 0 && (
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, fontSize: 13, color: 'text.secondary' }}>
                        Taken from <TakenFrom value={nowChips} compact />
                      </Box>
                    )}
                  </Box>
                )}
                {choice === 'hand' && canHand && (
                  <Box sx={{ border: `1px solid ${theme.tokens.line}`, borderRadius: 2, p: 1.5, display: 'grid', gap: 1 }}>
                    <Typography variant="body2" sx={{ fontWeight: 700 }}>
                      {preview.hand_editable ? 'Where were the days taken from? Balances stay as they are.' : 'These moves are recorded. Balances stay as they are.'}
                    </Typography>
                    {preview.hand_editable && (
                      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1.25 }}>
                        {HAND_POTS.map((pot) => (
                          <TextField
                            key={pot} label={POT_LABEL[pot]} size="small" type="number"
                            inputProps={{ min: 0, step: 0.5 }}
                            value={hand[pot] ?? ''}
                            onChange={(e) => setHand((h) => ({ ...h, [pot]: e.target.value }))}
                            sx={{ width: 120 }}
                          />
                        ))}
                      </Box>
                    )}
                    {preview.hand_editable && (
                      <Typography variant="body2" sx={{ fontWeight: 600, color: handOk ? 'text.secondary' : 'error.main' }}>
                        {handOk ? `Adds up to the request: ${fmtDays(days)} days.` : `These add up to ${fmtDays(handSum)}; the request is ${fmtDays(days)} days.`}
                      </Typography>
                    )}
                    {handChips.pieces.length > 0 && (
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, fontSize: 13, color: 'text.secondary' }}>
                        Taken from <TakenFrom value={handChips} compact />
                      </Box>
                    )}
                  </Box>
                )}
                {choice === 'none' && (
                  <Box sx={{ border: `1px solid ${theme.tokens.line}`, borderRadius: 2, p: 1.5 }}>
                    <Typography variant="body2">Nothing moves. The request will say <b>No balance needed</b>, with your reason.</Typography>
                  </Box>
                )}

                <TextField
                  label="Reason" required fullWidth autoFocus
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && canSave) { e.preventDefault(); save(); } }}
                  placeholder={choice === 'now' ? 'e.g. Approved in July, never deducted' : choice === 'hand' ? 'e.g. HR lowered vacation by hand on Sep 1' : 'e.g. Test request'}
                  helperText="Kept on the request's timeline, with your choice."
                />
              </>
            )}
          </>
        )}

        {onReview && (
          <Box sx={{ display: 'grid', gap: 1.25 }}>
            <Typography>
              {done.length === list.length
                ? `All ${list.length} ${list.length === 1 ? 'request is' : 'requests are'} settled. Each now shows where its days came from.`
                : `${done.length} of ${list.length} settled. Skipped requests stay flagged.`}
            </Typography>
            {done.map((d) => (
              <Box key={`${d.req.kind}:${d.req.item_id}`} sx={{ bgcolor: theme.tokens.chip, borderRadius: 2, p: 1.5, display: 'grid', gap: 0.25 }}>
                <Typography variant="body2" sx={{ fontWeight: 700 }}>
                  #{d.req.item_id} {kindLabel(d.req)}{d.req.start_date ? `, ${formatDayRange(d.req.start_date, d.req.end_date)}` : ''}
                </Typography>
                <Typography variant="body2">{d.outcome}</Typography>
                <Typography variant="body2" color="text.secondary">Reason: {d.reason}</Typography>
              </Box>
            ))}
          </Box>
        )}
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 1.5 }}>
        <Button onClick={() => onClose(done.map((d) => d.req))} sx={{ mr: 'auto' }}>{onReview ? 'Close' : 'Cancel'}</Button>
        {!onReview && <Button variant="outlined" onClick={next} disabled={saving}>Skip for now</Button>}
        {!onReview && (
          <Button
            variant="contained" onClick={save} disabled={!canSave}
            startIcon={saving ? <CircularProgress size={14} color="inherit" /> : undefined}
          >
            {idx === list.length - 1 ? 'Save and review' : 'Save and next'}
          </Button>
        )}
        {onReview && <Button variant="contained" onClick={() => onClose(done.map((d) => d.req))}>Done</Button>}
      </DialogActions>
    </Dialog>
  );
}
