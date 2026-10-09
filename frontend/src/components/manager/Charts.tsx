import { useState } from 'react';
import { Box, Button, Tooltip, Typography } from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import { kindColor } from '../../constants/leaveColors';
import { addDays, dayKey, isWeekend, today } from '../../utils/dates';
import { ColorOf, LabelOf } from '../../utils/personColor';
import { shortDate } from '../../utils/requestText';
import { Absence } from './teamStats';

/** Turns an animation off for anyone whose system asks for reduced motion. */
const CALM = { '@media (prefers-reduced-motion: reduce)': { animation: 'none' } };

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * A timeline: one row per person, one bar per absence spanning its days.
 * Approved time is a solid bar in the person's colour; time asked for but not
 * approved yet is dashed. Bars grow in from the left, one after another.
 *
 * By default only people with an absence in the window get a row, so a large
 * team stays short. A toggle (when asked for) switches between "only people
 * off" and the whole team; a row cap (when asked for) trims the list and offers
 * a "more" link instead of a long scroll.
 *
 * @param props.names - The team, one row each when everyone is shown.
 * @param props.absences - What to draw.
 * @param props.from - The first day shown.
 * @param props.days - How many days to show.
 * @param props.colorOf - Each person's colour.
 * @param props.labelOf - Each person's short, unambiguous label.
 * @param props.showToggle - Offer the "only off / everyone" toggle.
 * @param props.maxRows - Show at most this many rows, with a "more" link.
 * @param props.onShowMore - Called by the "more" link (e.g. open the Calendar tab).
 * @returns The timeline.
 */
export function LeaveTimeline({
  names, absences, from, days, colorOf, labelOf, showToggle = false, maxRows, onShowMore,
}: {
  names: string[]; absences: Absence[]; from: Date; days: number;
  colorOf: ColorOf; labelOf: LabelOf; showToggle?: boolean; maxRows?: number; onShowMore?: () => void;
}) {
  const theme = useTheme();
  const last = addDays(from, days - 1);
  const t = dayKey(today());
  const cols = Array.from({ length: days }, (_, i) => addDays(from, i));
  const pct = (i: number) => `${(i / days) * 100}%`;     // a day index as a share of the track
  let n = 0;                                               // running count, for the staggered entry

  // When the toggle is on, start on "only people off"; it can expand to everyone.
  const [onlyOff, setOnlyOff] = useState(true);
  // People with any absence overlapping the shown window.
  const offNames = names.filter((who) => absences.some((a) => a.who === who && a.end >= from && a.start <= last));
  // Rows to consider: the toggle picks the set; without a toggle we always show only the people off.
  const chosen = showToggle && !onlyOff ? names : offNames;
  // Trim to the cap, if any, and count what the cap hides.
  const shown = maxRows ? chosen.slice(0, maxRows) : chosen;
  const hiddenByCap = chosen.length - shown.length;
  // What to say when there is no row to draw.
  const emptyText = names.length === 0 ? 'No one on your team yet.' : 'No one is away in this window.';

  return (
    <Box sx={{ display: 'grid', gap: 1 }}>
      {/* The "only off / everyone" toggle, kept above the scroll area so it is always reachable. */}
      {showToggle && (
        <Button
          size="small" variant="text" onClick={() => setOnlyOff((v) => !v)} aria-pressed={!onlyOff}
          sx={{ justifySelf: 'start', textTransform: 'none' }}
        >
          {onlyOff ? `Show everyone (${names.length})` : 'Only people off'}
        </Button>
      )}

      {shown.length === 0 ? (
        // Nothing to draw: a plain line rather than an empty grid.
        <Typography variant="body2" color="text.secondary">{emptyText}</Typography>
      ) : (
      <Box sx={{ overflowX: 'auto' }}>
      <Box sx={{ minWidth: days > 14 ? 720 : 440, display: 'grid', gap: 0.75 }}>
        {/* Day header: weekday letter and date, weekends muted, today marked. */}
        <Box sx={{ display: 'grid', gridTemplateColumns: '96px 1fr', alignItems: 'end' }}>
          <span />
          <Box sx={{ display: 'grid', gridTemplateColumns: `repeat(${days}, 1fr)`, textAlign: 'center', fontSize: 11, lineHeight: 1.2 }}>
            {cols.map((d) => {
              const now = dayKey(d) === t;
              return (
                <Box key={dayKey(d)} sx={{ color: now ? 'primary.main' : isWeekend(d) ? 'text.disabled' : 'text.secondary', fontWeight: now ? 800 : 500 }}>
                  {'SMTWTFS'[d.getDay()]}<br />{d.getDate()}
                </Box>
              );
            })}
          </Box>
        </Box>

        {shown.map((who) => {
          const mine = absences.filter((a) => a.who === who && a.end >= from && a.start <= last);
          return (
            <Box key={who} sx={{ display: 'grid', gridTemplateColumns: '96px 1fr', alignItems: 'center' }}>
              <Typography variant="body2" noWrap sx={{ fontWeight: 600, display: 'flex', alignItems: 'center', gap: 0.75 }}>
                <Box component="span" sx={{ width: 8, height: 8, borderRadius: 999, bgcolor: colorOf(who), flex: 'none' }} />
                {labelOf(who)}
              </Typography>
              <Box sx={{ position: 'relative', height: 28, borderRadius: '8px', bgcolor: theme.tokens.hover }}>
                {/* Weekend shading. */}
                {cols.map((d, i) => isWeekend(d) && (
                  <Box key={i} sx={{ position: 'absolute', top: 0, bottom: 0, left: pct(i), width: `${100 / days}%`, bgcolor: alpha(theme.palette.text.primary, 0.05) }} />
                ))}
                {mine.map((a) => {
                  // Clip the bar to the visible window.
                  const s = Math.max(0, Math.round((a.start.getTime() - from.getTime()) / 86400000));
                  const e = Math.min(days - 1, Math.round((a.end.getTime() - from.getTime()) / 86400000));
                  const c = colorOf(who);
                  return (
                    <Tooltip key={a.key} title={`${who}: ${a.label}, ${shortDate(dayKey(a.start))}${a.end > a.start ? ` to ${shortDate(dayKey(a.end))}` : ''}${a.pending ? ' (asked, not approved yet)' : ''}`}>
                      <Box
                        sx={{
                          position: 'absolute', top: 3, bottom: 3, left: `calc(${pct(s)} + 2px)`, width: `calc(${((e - s + 1) / days) * 100}% - 4px)`,
                          borderRadius: '6px', px: 0.75, overflow: 'hidden', whiteSpace: 'nowrap', fontSize: 11, fontWeight: 700, lineHeight: '22px',
                          // Approved: solid colour. Asked: dashed outline in the same colour.
                          ...(a.pending
                            ? { color: c, border: `1.5px dashed ${c}`, bgcolor: alpha(c, 0.08) }
                            : { color: theme.palette.mode === 'dark' ? theme.palette.background.default : '#fff', bgcolor: c }),
                          transformOrigin: 'left', animation: `tlgrow .5s ${0.1 + 0.06 * n++}s both cubic-bezier(.2,.8,.2,1)`,
                          '@keyframes tlgrow': { from: { transform: 'scaleX(0)', opacity: 0 }, to: { transform: 'scaleX(1)', opacity: 1 } },
                          ...CALM,
                        }}
                      >
                        {a.label}
                      </Box>
                    </Tooltip>
                  );
                })}
                {/* Today's line. */}
                {cols.some((d) => dayKey(d) === t) && (
                  <Box sx={{ position: 'absolute', top: -2, bottom: -2, width: 2, borderRadius: 1, bgcolor: theme.tokens.accent, left: `calc(${pct(cols.findIndex((d) => dayKey(d) === t))} + ${50 / days}%)` }} />
                )}
              </Box>
            </Box>
          );
        })}
      </Box>
      </Box>
      )}

      {/* The cap hid some people off: a link to the fuller view rather than a long scroll. */}
      {hiddenByCap > 0 && onShowMore && (
        <Button size="small" variant="text" onClick={onShowMore} sx={{ justifySelf: 'start', textTransform: 'none' }}>
          +{hiddenByCap} more
        </Button>
      )}
    </Box>
  );
}

/**
 * A small line for a tile, drawing itself in.
 *
 * @param props.values - The points, oldest first (two or more).
 * @param props.height - Pixel height.
 * @returns The sparkline, or nothing with fewer than two points.
 */
export function Sparkline({ values, height = 30 }: { values: number[]; height?: number }) {
  const theme = useTheme();
  if (values.length < 2) return null;
  const W = 140, H = 30, max = Math.max(...values), min = Math.min(...values);
  const pts = values.map((v, k) => `${(k * W / (values.length - 1)).toFixed(1)},${(H - 3 - (v - min) / (max - min || 1) * (H - 6)).toFixed(1)}`).join(' ');
  return (
    <Box component="svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden sx={{ width: '100%', height, display: 'block', mt: 0.5 }}>
      <Box
        component="polyline" points={pts}
        sx={{
          fill: 'none', stroke: theme.palette.primary.main, strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round',
          strokeDasharray: 400, animation: 'draw 1.2s .2s both ease-out', '@keyframes draw': { from: { strokeDashoffset: 400 }, to: { strokeDashoffset: 0 } }, ...CALM,
        }}
      />
    </Box>
  );
}

/**
 * Who is in on each working day: a filled line with the usual cover marked
 * and the thin days dotted.
 *
 * @param props.points - {day, in} per working day.
 * @param props.team - Team size (the top of the scale).
 * @returns The chart.
 */
export function InPerDayChart({ points, team }: { points: { day: Date; in: number }[]; team: number }) {
  const theme = useTheme();
  if (points.length < 2 || team === 0) return null;
  const W = 700, H = 170, pad = 28;
  const x = (k: number) => pad + k * (W - pad * 2) / (points.length - 1);
  const y = (v: number) => H - pad - v / team * (H - pad * 2);
  const line = points.map((p, k) => `${k ? 'L' : 'M'}${x(k).toFixed(1)} ${y(p.in).toFixed(1)}`).join(' ');
  const area = `${line} L${x(points.length - 1)} ${H - pad} L${x(0)} ${H - pad} Z`;
  const usual = Math.max(0, team - 1);                   // one person away is a normal day
  const ink = theme.palette.text.secondary;
  return (
    <Box component="svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="People in per working day" sx={{ width: '100%', height: 'auto', display: 'block', fontSize: 9, fill: ink }}>
      {[0, team / 2, team].map((v) => (
        <g key={v}>
          <line x1={pad} x2={W - pad} y1={y(v)} y2={y(v)} stroke={theme.palette.divider} />
          <text x={0} y={y(v) + 4}>{Math.round(v)}</text>
        </g>
      ))}
      <line x1={pad} x2={W - pad} y1={y(usual)} y2={y(usual)} stroke={theme.tokens.accent} strokeDasharray="4 4" />
      <text x={W - pad} y={y(usual) - 6} textAnchor="end" fill={theme.tokens.accent}>usual cover: {usual}</text>
      <path d={area} fill={alpha(theme.palette.primary.main, 0.12)} />
      <Box component="path" d={line} sx={{ fill: 'none', stroke: theme.palette.primary.main, strokeWidth: 2.5, strokeDasharray: 2000, animation: 'draw 1.4s both ease-out', '@keyframes draw': { from: { strokeDashoffset: 2000 }, to: { strokeDashoffset: 0 } }, ...CALM }} />
      {points.map((p, k) => p.in < usual && (
        <circle key={k} cx={x(k)} cy={y(p.in)} r={5} fill={theme.tokens.accent}>
          <title>{`${shortDate(dayKey(p.day))}: ${p.in} of ${team} in`}</title>
        </circle>
      ))}
      {points.map((p, k) => k % 3 === 0 && (
        <text key={`l${k}`} x={x(k)} y={H - 6} textAnchor="middle">{shortDate(dayKey(p.day))}</text>
      ))}
    </Box>
  );
}

/**
 * Stacked bars of days off per month (vacation, sick, other), growing up in turn.
 *
 * @param props.months - Twelve {vacation, sick, other} totals.
 * @param props.upTo - Show months up to this index (0 = January).
 * @returns The chart.
 */
export function MonthlyBars({ months, upTo }: { months: { vacation: number; sick: number; other: number }[]; upTo: number }) {
  const theme = useTheme();
  const mode = theme.palette.mode;
  const shown = months.slice(0, upTo + 1);
  const W = 700, H = 200, pad = 28, n = shown.length;
  const top = Math.max(4, ...shown.map((m) => m.vacation + m.sick + m.other));
  const max = Math.ceil(top / 4) * 4;                    // round the scale up to a multiple of 4
  const bw = (W - pad * 2) / n * 0.56;
  const y = (v: number) => (H - pad) - v / max * (H - pad * 2);
  const fills = { vacation: kindColor('vacation', mode), sick: kindColor('sick', mode), other: kindColor('bereavement', mode) };
  return (
    <Box component="svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Days off per month" sx={{ width: '100%', height: 'auto', display: 'block', fontSize: 9, fill: theme.palette.text.secondary }}>
      {[0, max / 4, max / 2, (3 * max) / 4, max].map((v) => (
        <g key={v}>
          <line x1={pad} x2={W - pad} y1={y(v)} y2={y(v)} stroke={theme.palette.divider} />
          <text x={0} y={y(v) + 4}>{v}</text>
        </g>
      ))}
      {shown.map((m, k) => {
        const cx = pad + (k + 0.5) * (W - pad * 2) / n;
        let base = 0;
        return (
          <g key={k}>
            {(['vacation', 'sick', 'other'] as const).map((s) => {
              const v = m[s];
              if (!v) return null;
              const y1 = y(base + v), h = y(base) - y1;
              base += v;
              return (
                <Box
                  component="rect" key={s} x={cx - bw / 2} y={y1} width={bw} height={h} rx={3} fill={fills[s]}
                  sx={{ transformBox: 'fill-box', transformOrigin: 'bottom', animation: `rise .6s ${k * 0.05}s both ease-out`, '@keyframes rise': { from: { transform: 'scaleY(0)' }, to: { transform: 'scaleY(1)' } }, ...CALM }}
                >
                  <title>{`${MONTHS[k]}: ${v} day${v === 1 ? '' : 's'} ${s}`}</title>
                </Box>
              );
            })}
            <text x={cx} y={H - 8} textAnchor="middle">{MONTHS[k]}</text>
          </g>
        );
      })}
    </Box>
  );
}

/**
 * A ring per person showing vacation left out of the year's entitlement,
 * drawing round as it appears.
 *
 * @param props.members - /team/members (balances.vacation_balance and vacation_entitlement).
 * @param props.colorOf - Each person's colour.
 * @param props.labelOf - Each person's short, unambiguous label.
 * @returns The rings.
 */
export function VacationRings({ members, colorOf, labelOf }: { members: any[]; colorOf: ColorOf; labelOf: LabelOf }) {
  const theme = useTheme();
  const r = 38, c = 2 * Math.PI * r;
  // One scrollable strip instead of N wrapped rows, so the panel stays one row tall on any team size.
  if (members.length === 0) return <Typography variant="body2" color="text.secondary">No matches.</Typography>;
  return (
    <Box sx={{ display: 'flex', gap: 2, overflowX: 'auto', pb: 1 }}>
      {members.map((m, k) => {
        const left = m.balances?.vacation_balance ?? 0;
        const of = m.balances?.vacation_entitlement ?? 0;
        const frac = of > 0 ? Math.max(0, Math.min(1, left / of)) : 0;
        return (
          <Box key={m.id ?? m.name} sx={{ flex: 'none', width: 118, display: 'grid', justifyItems: 'center', textAlign: 'center', gap: 0.25 }}>
            <Box sx={{ position: 'relative', width: 92, height: 92 }}>
              <svg viewBox="0 0 92 92" width={92} height={92} aria-hidden>
                <circle cx={46} cy={46} r={r} fill="none" stroke={theme.tokens.hover} strokeWidth={9} />
                <Box
                  component="circle" cx={46} cy={46} r={r} fill="none" stroke={colorOf(m.name)} strokeWidth={9} strokeLinecap="round"
                  strokeDasharray={c} strokeDashoffset={c * (1 - frac)} transform="rotate(-90 46 46)"
                  sx={{ animation: `ring 1s ${k * 0.1}s both ease-out`, '@keyframes ring': { from: { strokeDashoffset: c } }, ...CALM }}
                />
              </svg>
              <Box sx={{ position: 'absolute', inset: 0, display: 'grid', placeContent: 'center', lineHeight: 1.1 }}>
                <Typography sx={{ fontFamily: theme.tokens.display, fontWeight: 600, fontSize: 20, color: left < 0 ? 'error.main' : 'text.primary' }}>{left}</Typography>
                {of > 0 && <Typography variant="caption" color="text.secondary">of {of}</Typography>}
              </Box>
            </Box>
            <Typography variant="body2" sx={{ fontWeight: 700 }} noWrap>{labelOf(m.name)}</Typography>
            <Typography variant="caption" color="text.secondary">vacation left</Typography>
          </Box>
        );
      })}
    </Box>
  );
}

/**
 * A balance bar showing what approval would use (or add): the kept part
 * solid, the change striped in the warn colour (or the success colour when it
 * goes up).
 *
 * @param props.cur - The balance now.
 * @param props.next - The balance after approval.
 * @param props.max - The bar's full length (the entitlement, where known).
 * @returns The meter.
 */
export function BalanceMeter({ cur, next, max }: { cur: number; next: number; max: number }) {
  const theme = useTheme();
  const top = Math.max(max, cur, next, 1);
  const lo = Math.max(0, Math.min(cur, next)), hi = Math.max(0, Math.max(cur, next));
  const change = next < cur ? theme.palette.warning.main : theme.palette.success.main;
  return (
    <Box sx={{ position: 'relative', height: 8, borderRadius: 999, bgcolor: theme.tokens.hover, overflow: 'hidden' }}>
      <Box sx={{ position: 'absolute', inset: 0, right: 'auto', width: `${(lo / top) * 100}%`, bgcolor: theme.palette.primary.main, borderRadius: 999 }} />
      <Box
        sx={{
          position: 'absolute', top: 0, bottom: 0, left: `${(lo / top) * 100}%`, width: `${((hi - lo) / top) * 100}%`,
          background: `repeating-linear-gradient(45deg, ${change}, ${change} 3px, ${alpha(change, 0.45)} 3px, ${alpha(change, 0.45)} 6px)`,
          transformOrigin: 'left', animation: 'tlgrow .6s .2s both ease-out', '@keyframes tlgrow': { from: { transform: 'scaleX(0)' }, to: { transform: 'scaleX(1)' } }, ...CALM,
        }}
      />
    </Box>
  );
}
