import { Fragment, ReactNode } from 'react';
import { Box, Typography } from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import { kindColor, RequestKind } from '../constants/leaveColors';
import { TakenFromValue, describeTakenFrom, fmtDays, potKind, potLabel } from '../utils/takenFrom';

interface Props {
  /** The row's taken_from, as the backend stamps it (or null/undefined when absent). */
  value: TakenFromValue | null | undefined;
  /** Tighter sizing for dense cells (history and admin tables, overview rows, cards). */
  compact?: boolean;
}

/**
 * One balance chip: a bold amount then a pot label, in that pot's colour. A
 * projected chip is dashed and unfilled (a guess, not a record); a refunded one
 * is struck through and dimmed (the days went back).
 *
 * @param props.kind - The leave-colour kind the chip borrows.
 * @param props.amount - The bold amount, or omitted for a destination chip ("-> ...").
 * @param props.label - The pot label (with its year when it has one).
 * @param props.projected - Draw it as a projection (dashed, unfilled).
 * @param props.refunded - Draw it as reversed (struck through, dimmed).
 * @param props.compact - Smaller text for dense rows.
 * @returns The chip.
 */
function Chip({ kind, amount, label, projected, refunded, compact }: {
  kind: RequestKind; amount?: string; label: string; projected: boolean; refunded: boolean; compact?: boolean;
}) {
  const theme = useTheme();
  const c = kindColor(kind, theme.palette.mode);                 // the pot's shade for light or dark
  return (
    <Box
      component="span"
      sx={{
        display: 'inline-flex', alignItems: 'baseline', gap: 0.5, whiteSpace: 'nowrap',
        px: compact ? 0.75 : 1, py: 0.25, borderRadius: '7px',
        fontSize: compact ? 11.5 : 12.5, fontWeight: 600, color: c,
        // Projected: dashed outline, no fill. Recorded/refunded: soft tint behind the text.
        bgcolor: projected ? 'transparent' : alpha(c, theme.palette.mode === 'dark' ? 0.18 : 0.12),
        border: projected ? `1px dashed ${c}` : '1px solid transparent',
        textDecoration: refunded ? 'line-through' : 'none',
        opacity: refunded ? 0.75 : 1,
      }}
    >
      {amount && <Box component="b" sx={{ fontVariantNumeric: 'tabular-nums', fontWeight: 700 }}>{amount}</Box>}
      {label}
    </Box>
  );
}

/**
 * The shared "Taken from" display (#296): which balance pots a request's days
 * came out of (approved), or would if approved today (pending). Used by the
 * employee and manager History tables, the employee overview lists, the request
 * page summary, the manager Today cards and the admin requests table, so every
 * screen reads the same way. All the wording and ordering lives in
 * utils/takenFrom.ts; this only draws it.
 *
 * @param props - See {@link Props}.
 * @returns The chips (with a note for a projection or a refund), or a muted line.
 */
export default function TakenFrom({ value, compact }: Props) {
  const shape = describeTakenFrom(value);                        // the pure read of the value

  // Nothing moved, not recorded, or would be refused: one muted line, no chips.
  if (shape.kind === 'message') {
    return (
      <Typography component="span" variant="body2" color="text.secondary" sx={{ fontStyle: 'italic', fontSize: compact ? 12 : 13 }}>
        {shape.text}
      </Typography>
    );
  }

  const { segments, projected, refunded } = shape;
  const noteFont = compact ? 11 : 11.5;                          // the sub-note sits on its own line

  return (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, alignItems: 'baseline' }}>
      {segments.map((s, i) => {
        let chips: ReactNode;
        if (s.type === 'add') {
          // Overtime: "+0.4 added to Make-Up".
          chips = <Chip kind={potKind(s.pot)} amount={`+${fmtDays(s.days)}`} label={`added to ${potLabel(s.pot, null)}`} projected={projected} refunded={refunded} compact={compact} />;
        } else if (s.type === 'move') {
          // Carry-over/payout: vacation moving into the pot, e.g. "3 vacation -> 2026 carry-over".
          chips = (
            <>
              <Chip kind="vacation" amount={fmtDays(s.days)} label="vacation" projected={projected} refunded={refunded} compact={compact} />
              <Chip kind={potKind(s.toPot)} label={`→ ${potLabel(s.toPot, s.toYear)}`} projected={projected} refunded={refunded} compact={compact} />
            </>
          );
        } else {
          // A plain take: "1.5 Make-Up", "2 2025 carry-over", "1 vacation".
          chips = <Chip kind={potKind(s.pot)} amount={fmtDays(s.days)} label={potLabel(s.pot, s.year)} projected={projected} refunded={refunded} compact={compact} />;
        }
        return <Fragment key={i}>{chips}</Fragment>;
      })}
      {/* A projection and a refund each explain themselves on their own line. */}
      {projected && (
        <Typography component="span" variant="caption" color="text.secondary" sx={{ width: '100%', fontSize: noteFont }}>If approved today</Typography>
      )}
      {refunded && (
        <Typography component="span" variant="caption" color="text.secondary" sx={{ width: '100%', fontSize: noteFont }}>Refunded</Typography>
      )}
    </Box>
  );
}
