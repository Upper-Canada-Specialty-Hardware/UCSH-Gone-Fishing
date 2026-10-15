import { ReactNode, useEffect, useState } from 'react';
import { Alert, Box, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { Balances, EmployeeSession, RequestPreview, RequestType, previewMyRequest } from '../../api/intake';
import { AutoHeight, CountUp, Fold, riseAt } from '../Motion';
import TakenFrom from '../TakenFrom';
import { describeTakenFrom, takenFromPreview } from '../../utils/takenFrom';
import { today } from '../../utils/dates';

/** The balances shown, in the order spending uses them. */
const POTS: { key: keyof Balances; label: string }[] = [
  { key: 'overtime', label: 'Make-Up' },
  { key: 'sick_balance', label: 'Sick' },
  { key: 'carryover', label: 'Carry Over' },
  { key: 'vacation_balance', label: 'Vacation' },
  { key: 'payout', label: 'Payout' },
];

const WAIT_MS = 400;   // pause after the last keystroke before asking the server

interface Props {
  session: EmployeeSession;
  type: RequestType;
  /** The form body as it would be sent; null while the form is not ready. */
  body: Record<string, unknown> | null;
}

/**
 * Under the form: the days the request counts and each balance now and after
 * approval, worked out by the server with the same rules approval uses. Only
 * balances that change are listed (Vacation always, as the one people watch).
 * Shows nothing until the form is ready, and nothing if the preview fails:
 * it is a help, never a blocker.
 *
 * @param props - See {@link Props}.
 * @returns The preview box, or nothing.
 */
export default function BalancePreview({ session, type, body }: Props) {
  const theme = useTheme();
  const [preview, setPreview] = useState<RequestPreview | null>(null);
  const key = body ? JSON.stringify(body) : '';         // a stable value for the effect to watch

  useEffect(() => {
    if (!key) { setPreview(null); return; }
    let live = true;                                     // ignore answers to an older form
    const t = setTimeout(() => {
      previewMyRequest(session, type, JSON.parse(key))
        .then((res) => live && setPreview(res.data))
        .catch(() => live && setPreview(null));          // a failed preview just hides
    }, WAIT_MS);
    return () => { live = false; clearTimeout(t); };
  }, [session, type, key]);

  // The amount line: leave counts working days, overtime adds days, carry-over moves them.
  const amountLabel = type === 'leave' ? 'Working days' : type === 'overtime' ? 'Adds (8 hours = 1 day)' : 'Days';
  const current = preview?.current;
  const projected = preview?.projected;
  const changed = current && projected ? POTS.filter((p) => p.key === 'vacation_balance' || current[p.key] !== projected[p.key]) : [];
  // The same split the dashboards show, worked out from the preview's current vs projected
  // balances (there is no server taken_from for an unsent request). Shown only when the
  // request actually moves a balance; a no-change case is already covered by `unchanged`.
  const taken = current && projected ? takenFromPreview(current, projected, today()) : null;
  const showTaken = taken ? describeTakenFrom(taken).kind === 'segments' : false;

  // The box folds open once the form is ready and follows its content's height;
  // each balance counts from now to after approval, so the change is felt.
  return (
    <Fold show={!!preview}>
      <AutoHeight>
        {preview && current && (
          <Box
            aria-live="polite"
            sx={{ border: `1px dashed ${theme.tokens.line2}`, borderRadius: '12px', px: 1.75, py: 1.5, display: 'grid', gap: 0.75, fontSize: 13 }}
          >
            <Row i={0} label={amountLabel} value={fmt(preview.days)} bold />
            {/* Where the days come from, as chips, before the balance-by-balance change below. */}
            {showTaken && (
              <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 1.25, alignItems: 'baseline', ...riseAt(1, { delay: 80, step: 60, dur: 300 }) }}>
                <Typography variant="body2" color="text.secondary">Taken from</Typography>
                <Box sx={{ display: 'flex', justifyContent: 'flex-end', flex: 1, minWidth: 0 }}><TakenFrom value={taken} compact /></Box>
              </Box>
            )}
            {changed.map((p, i) => (
              <Row
                key={p.key} i={i + 1} label={p.label} negative={projected![p.key] < 0}
                value={<>{fmt(current[p.key])} → <CountUp from={current[p.key]} value={projected![p.key]} format={fmt} delay={0.15 + i * 0.06} /></>}
              />
            ))}
            {preview.unchanged && <Typography variant="body2" color="text.secondary">{preview.unchanged}</Typography>}
            {preview.notes.map((n) => <Typography key={n} variant="body2" color="text.secondary">{n}</Typography>)}
            {preview.warning && <Alert severity="warning" sx={{ mt: 0.5 }}>{preview.warning}</Alert>}
          </Box>
        )}
      </AutoHeight>
    </Fold>
  );
}

/**
 * A number without trailing zeros, e.g. 2, 0.5, -1.5.
 *
 * @param n - Days.
 * @returns The text.
 */
function fmt(n: number): string {
  return String(Math.round(n * 100) / 100);
}

/**
 * One label and value, spread across the box.
 *
 * @param props.label - Left.
 * @param props.value - Right.
 * @param props.bold - Emphasise the value.
 * @param props.negative - Show the value in the error colour (below zero).
 * @param props.i - Its place, for the rise-in.
 * @returns The row.
 */
function Row({ label, value, bold, negative, i }: { label: string; value: ReactNode; bold?: boolean; negative?: boolean; i: number }) {
  return (
    <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 1.25, ...riseAt(i, { delay: 80, step: 60, dur: 300 }) }}>
      <Typography variant="body2" color="text.secondary">{label}</Typography>
      <Typography variant="body2" sx={{ fontWeight: bold ? 700 : 600, fontVariantNumeric: 'tabular-nums', color: negative ? 'error.main' : 'text.primary', transition: 'color .3s ease' }}>
        {value}
      </Typography>
    </Box>
  );
}
