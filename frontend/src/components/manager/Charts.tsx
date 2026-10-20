import { useState } from 'react';
import { Box, Button, Chip, IconButton, Tooltip, Typography } from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import CloseIcon from '@mui/icons-material/Close';
import { AnimatePresence, m } from 'motion/react';
import KindPill from '../KindPill';
import { kindColor } from '../../constants/leaveColors';
import { addDays, dayKey, formatDay, isWeekend, today } from '../../utils/dates';
import { ColorOf, LabelOf } from '../../utils/personColor';
import { isHourly } from '../../utils/employment';
import { shortDate } from '../../utils/requestText';
import { Absence } from './teamStats';
import { CountUp, EASE, EXIT, INSTANT, KF, SPRING, useCalm } from '../Motion';

/** Ease-out cubic, for a drawn line whose progress the dots below follow. */
const DRAW_EASE = 'cubic-bezier(.33,1,.68,1)';

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
 * @param props.onSelectAbsence - When set, each bar becomes a button that selects
 *   its absence (#373); keyboard reachable with an aria-label of the figures.
 * @param props.selectedKey - The selected absence's key, drawn with a ring.
 * @returns The timeline.
 */
export function LeaveTimeline({
  names, absences, from, days, colorOf, labelOf, showToggle = false, maxRows, onShowMore,
  onSelectAbsence, selectedKey,
}: {
  names: string[]; absences: Absence[]; from: Date; days: number;
  colorOf: ColorOf; labelOf: LabelOf; showToggle?: boolean; maxRows?: number; onShowMore?: () => void;
  onSelectAbsence?: (a: Absence) => void; selectedKey?: string | null;
}) {
  const theme = useTheme();
  const calm = useCalm();
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
  // How many bars grow in, so today's line can drop in after the last one.
  const barCount = shown.reduce((sum, who) => sum + absences.filter((a) => a.who === who && a.end >= from && a.start <= last).length, 0);

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
      // Room above and below for today's line, so it never adds a vertical scrollbar.
      <Box sx={{ overflowX: 'auto', overflowY: 'hidden', py: '2px' }}>
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

        {/* Rows fold in and out when the set changes (Show everyone), so the others close up smoothly. */}
        <AnimatePresence initial={false}>
        {shown.map((who) => {
          const mine = absences.filter((a) => a.who === who && a.end >= from && a.start <= last);
          return (
            <m.div
              key={who}
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0, transition: calm ? INSTANT : EXIT }}
              transition={calm ? INSTANT : SPRING}
              style={{ display: 'grid', gridTemplateColumns: '96px 1fr', alignItems: 'center' }}
            >
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
                  const interactive = Boolean(onSelectAbsence);          // bars click through only when asked (#373)
                  const selected = selectedKey === a.key;                // the open bar gets a ring
                  // The spoken label carries the figures (who, kind, dates, state).
                  const aria = `${who}: ${a.label}, ${shortDate(dayKey(a.start))}${a.end > a.start ? ` to ${shortDate(dayKey(a.end))}` : ''}${a.pending ? ', asked, waiting' : ', approved'}`;
                  return (
                    <Tooltip key={a.key} title={`${who}: ${a.label}, ${shortDate(dayKey(a.start))}${a.end > a.start ? ` to ${shortDate(dayKey(a.end))}` : ''}${a.pending ? ' (asked, not approved yet)' : ''}`}>
                      <Box
                        role={interactive ? 'button' : undefined}
                        tabIndex={interactive ? 0 : undefined}
                        aria-label={interactive ? aria : undefined}
                        aria-pressed={interactive ? selected : undefined}
                        onClick={interactive ? () => onSelectAbsence!(a) : undefined}
                        onKeyDown={interactive ? (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onSelectAbsence!(a); } } : undefined}
                        sx={{
                          position: 'absolute', top: 3, bottom: 3, left: `calc(${pct(s)} + 2px)`, width: `calc(${((e - s + 1) / days) * 100}% - 4px)`,
                          borderRadius: '6px', px: 0.75, overflow: 'hidden', whiteSpace: 'nowrap', fontSize: 11, fontWeight: 700, lineHeight: '22px',
                          cursor: interactive ? 'pointer' : 'default',
                          // Approved: solid colour. Asked: dashed outline in the same colour (unfilled).
                          ...(a.pending
                            ? { color: c, border: `1.5px dashed ${c}`, bgcolor: alpha(c, 0.08) }
                            : { color: theme.palette.mode === 'dark' ? theme.palette.background.default : '#fff', bgcolor: c }),
                          // The selected bar carries a ring in the accent colour.
                          ...(selected ? { outline: `2px solid ${theme.tokens.accent}`, outlineOffset: 1 } : {}),
                          transformOrigin: 'left', animation: `${KF.growXFade} .5s ${EASE.out} ${0.1 + 0.06 * n++}s both`,
                        }}
                      >
                        {a.label}
                      </Box>
                    </Tooltip>
                  );
                })}
                {/* Today's line, dropping in once the bars have grown. */}
                {cols.some((d) => dayKey(d) === t) && (
                  <Box sx={{
                    position: 'absolute', top: -2, bottom: -2, width: 2, borderRadius: 1, bgcolor: theme.tokens.accent,
                    left: `calc(${pct(cols.findIndex((d) => dayKey(d) === t))} + ${50 / days}%)`,
                    transformOrigin: 'top', animation: `${KF.growY} .3s ${EASE.out} ${0.35 + 0.06 * barCount}s both`,
                  }} />
                )}
              </Box>
            </m.div>
          );
        })}
        </AnimatePresence>
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
 * The detail card under the Calendar timeline for a selected leave bar (#373):
 * the person, the kind and days, the full date range read "Nov 2, 2026 to
 * Nov 6, 2026", and whether it is approved or still asked. It offers Approve and
 * Reject (only while pending), "Open in History", and "Open <first name>" to the
 * person profile. The parent owns what each action does.
 *
 * @param props.absence - The selected absence.
 * @param props.days - The request's day count, or null when unknown.
 * @param props.colorOf - The person's avatar colour.
 * @param props.processingEnabled - Whether writes are on (gates Approve/Reject).
 * @param props.busy - Whether a decision on this request is in flight.
 * @param props.openPersonLabel - The "Open <first name>" button label.
 * @param props.onApprove - Approve the request.
 * @param props.onReject - Reject the request.
 * @param props.onOpenHistory - Open the request's row in History.
 * @param props.onOpenPerson - Open the person's profile (#372).
 * @param props.onClose - Close the card.
 * @returns The detail card.
 */
export function AbsenceDetail({
  absence, days, colorOf, processingEnabled, busy, openPersonLabel,
  onApprove, onReject, onOpenHistory, onOpenPerson, onClose,
}: {
  absence: Absence; days: number | null; colorOf: ColorOf; processingEnabled: boolean; busy: boolean;
  openPersonLabel: string; onApprove: () => void; onReject: () => void;
  onOpenHistory: () => void; onOpenPerson: () => void; onClose: () => void;
}) {
  const theme = useTheme();
  const a = absence;
  const letters = (a.who || '').split(' ').map((w) => w[0] || '').slice(0, 2).join('');  // avatar initials
  // The full range always carries both years, as the brief asks ("Nov 2, 2026 to
  // Nov 6, 2026"); formatDayRange would drop the first year in the same year.
  const range = dayKey(a.start) === dayKey(a.end)
    ? formatDay(dayKey(a.start))
    : `${formatDay(dayKey(a.start))} to ${formatDay(dayKey(a.end))}`;
  const dayText = days != null ? `${days} day${days === 1 ? '' : 's'}` : '';   // "3 days", or nothing
  return (
    <Box sx={{ mt: 2, p: 2, border: 1, borderColor: theme.tokens.line2, borderRadius: 2, display: 'grid', gap: 1.25 }}>
      {/* Head: avatar, name, close. */}
      <Box sx={{ display: 'flex', gap: 1.25, alignItems: 'center' }}>
        <Box sx={{ width: 34, height: 34, borderRadius: 999, flex: 'none', bgcolor: colorOf(a.who), color: theme.palette.mode === 'dark' ? theme.palette.background.default : '#fff', display: 'grid', placeItems: 'center', fontWeight: 700, fontSize: 12.5 }}>
          {letters}
        </Box>
        <Typography sx={{ fontWeight: 700, flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}>{a.who}</Typography>
        <IconButton size="small" aria-label="Close" onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
      </Box>
      {/* Kind, days and the full date range. */}
      <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
        {a.kind && <KindPill kind={a.kind} label={a.label} />}
        {dayText && <Typography variant="body2" color="text.secondary">{dayText}</Typography>}
      </Box>
      <Typography sx={{ fontWeight: 600 }}>{range}</Typography>
      {/* Approved, or asked and sitting on this manager. */}
      <Chip
        size="small" label={a.pending ? 'Asked, waiting on you' : 'Approved'}
        color={a.pending ? 'warning' : 'success'} variant={a.pending ? 'outlined' : 'filled'}
        sx={{ justifySelf: 'start' }}
      />
      {/* Actions: Approve/Reject only while pending, then the two "open" links. */}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        {a.pending && (
          <>
            <Button size="small" variant="contained" color="success" disabled={busy || !processingEnabled} onClick={onApprove}>
              {busy ? 'Working...' : 'Approve'}
            </Button>
            <Button size="small" variant="outlined" color="error" disabled={busy || !processingEnabled} onClick={onReject}>Reject</Button>
          </>
        )}
        <Button size="small" variant="outlined" onClick={onOpenHistory}>Open in History</Button>
        <Button size="small" variant="outlined" onClick={onOpenPerson}>{openPersonLabel}</Button>
      </Box>
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
        component="polyline" points={pts} pathLength={1}
        sx={{
          fill: 'none', stroke: theme.palette.primary.main, strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round',
          strokeDasharray: 1, animation: `${KF.draw} 1.2s ease-out .2s both`,
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
 * @param props.onPickDay - When set, each day is a button that picks it (#373);
 *   keyboard reachable, with an aria-label of the figures.
 * @param props.pickedDay - The picked day, drawn with a faint band.
 * @returns The chart.
 */
export function InPerDayChart({ points, team, onPickDay, pickedDay }: {
  points: { day: Date; in: number }[]; team: number;
  onPickDay?: (day: Date) => void; pickedDay?: Date | null;
}) {
  const theme = useTheme();
  if (points.length < 2 || team === 0) return null;
  const W = 700, H = 170, pad = 28;
  const x = (k: number) => pad + k * (W - pad * 2) / (points.length - 1);
  const y = (v: number) => H - pad - v / team * (H - pad * 2);
  const half = (W - pad * 2) / (points.length - 1) / 2;   // half the gap between days, for the hit area
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
      <Box component="path" d={area} fill={alpha(theme.palette.primary.main, 0.12)} sx={{ animation: `${KF.fade} .5s ease-out .7s both` }} />
      <Box component="path" d={line} pathLength={1} sx={{ fill: 'none', stroke: theme.palette.primary.main, strokeWidth: 2.5, strokeDasharray: 1, animation: `${KF.draw} 1.4s ${DRAW_EASE} both` }} />
      {/* Each thin day pops in as the line reaches it. */}
      {points.map((p, k) => p.in < usual && (
        <Box
          component="circle" key={k} cx={x(k)} cy={y(p.in)} r={5} fill={theme.tokens.accent}
          sx={{ transformBox: 'fill-box', transformOrigin: 'center', animation: `${KF.popIn} .45s cubic-bezier(.2,1.6,.4,1) ${1.4 * (1 - Math.cbrt(1 - k / (points.length - 1)))}s both` }}
        >
          <title>{`${shortDate(dayKey(p.day))}: ${p.in} of ${team} in`}</title>
        </Box>
      ))}
      {points.map((p, k) => k % 3 === 0 && (
        <text key={`l${k}`} x={x(k)} y={H - 6} textAnchor="middle">{shortDate(dayKey(p.day))}</text>
      ))}
      {/* Clickable, keyboard-reachable hit area per working day (#373): a faint
          band marks the picked day; the transparent rect catches the click. */}
      {onPickDay && points.map((p, k) => {
        const picked = pickedDay != null && dayKey(pickedDay) === dayKey(p.day);
        return (
          <g key={`hit-${k}`}>
            {picked && <rect x={x(k) - half} y={pad} width={half * 2} height={H - pad * 2} fill={alpha(theme.tokens.accent, 0.12)} rx={4} />}
            <Box
              component="rect" x={x(k) - half} y={0} width={half * 2} height={H} fill="transparent"
              role="button" tabIndex={0}
              aria-label={`${shortDate(dayKey(p.day))}: ${p.in} of ${team} in. Show who is off.`}
              onClick={() => onPickDay(p.day)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPickDay(p.day); } }}
              sx={{ cursor: 'pointer', '&:focus-visible': { outline: `2px solid ${theme.tokens.accent}` } }}
            />
          </g>
        );
      })}
    </Box>
  );
}

/**
 * Stacked bars of days off per month (vacation, sick, other), growing up in turn.
 *
 * @param props.months - Twelve {vacation, sick, other} totals.
 * @param props.upTo - Show months up to this index (0 = January).
 * @param props.onPickMonth - When set, each month is a button that picks it by
 *   index (#373); keyboard reachable, with an aria-label of the month's total.
 * @returns The chart.
 */
export function MonthlyBars({ months, upTo, onPickMonth }: {
  months: { vacation: number; sick: number; other: number }[]; upTo: number;
  onPickMonth?: (monthIndex: number) => void;
}) {
  const theme = useTheme();
  const mode = theme.palette.mode;
  const shown = months.slice(0, upTo + 1);
  const W = 700, H = 200, pad = 28, n = shown.length;
  const colW = (W - pad * 2) / n;                          // one month's column width, for the hit area
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
                  sx={{ transformBox: 'fill-box', transformOrigin: 'bottom', animation: `${KF.growY} .6s ease-out ${k * 0.05}s both` }}
                >
                  <title>{`${MONTHS[k]}: ${v} day${v === 1 ? '' : 's'} ${s}`}</title>
                </Box>
              );
            })}
            <text x={cx} y={H - 8} textAnchor="middle">{MONTHS[k]}</text>
          </g>
        );
      })}
      {/* Clickable, keyboard-reachable hit area per month (#373): the transparent
          rect over the whole column opens that month's leave in History. */}
      {onPickMonth && shown.map((m, k) => {
        const total = Math.round((m.vacation + m.sick + m.other) * 10) / 10;
        return (
          <Box
            component="rect" key={`hit-${k}`} x={pad + k * colW} y={0} width={colW} height={H} fill="transparent"
            role="button" tabIndex={0}
            aria-label={`${MONTHS[k]}: ${total} day${total === 1 ? '' : 's'} off. Open in History.`}
            onClick={() => onPickMonth(k)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPickMonth(k); } }}
            sx={{ cursor: 'pointer', '&:focus-visible': { outline: `2px solid ${theme.tokens.accent}` } }}
          />
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
  const r = 38;
  // One scrollable strip instead of N wrapped rows, so the panel stays one row tall on any team size.
  if (members.length === 0) return <Typography variant="body2" color="text.secondary">No matches.</Typography>;
  return (
    <Box sx={{ display: 'flex', gap: 2, overflowX: 'auto', pb: 1, position: 'relative' }}>
      {/* A search fades out the people who no longer match while the rest slide together. */}
      <AnimatePresence mode="popLayout" initial={false}>
      {members.map((mem, k) => {
        // hourly staff have no vacation balance: the ring shows their counter ("taken") with no arc (#300).
        const bc = isHourly(mem.balances);
        const left = bc ? (mem.balances?.vacation_taken ?? 0) : (mem.balances?.vacation_balance ?? 0);
        const of = bc ? 0 : (mem.balances?.vacation_entitlement ?? 0);
        const frac = of > 0 ? Math.max(0, Math.min(1, left / of)) : 0;
        return (
          <m.div
            key={mem.id ?? mem.name}
            layout
            initial={{ opacity: 0, scale: 0.94 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.94, transition: EXIT }}
            style={{ flex: 'none', width: 118, display: 'grid', justifyItems: 'center', textAlign: 'center', gap: 2 }}
          >
            <Box sx={{ position: 'relative', width: 92, height: 92 }}>
              <svg viewBox="0 0 92 92" width={92} height={92} aria-hidden>
                <circle cx={46} cy={46} r={r} fill="none" stroke={theme.tokens.hover} strokeWidth={9} />
                <Box
                  component="circle" cx={46} cy={46} r={r} fill="none" stroke={colorOf(mem.name)} strokeWidth={9} strokeLinecap="round"
                  pathLength={1} strokeDasharray={1} strokeDashoffset={1 - frac} transform="rotate(-90 46 46)"
                  sx={{ animation: `${KF.draw} 1s ease-out ${k * 0.1}s both` }}
                />
              </svg>
              <Box sx={{ position: 'absolute', inset: 0, display: 'grid', placeContent: 'center', lineHeight: 1.1 }}>
                {/* The days count up as the ring fills. */}
                <Typography sx={{ fontFamily: theme.tokens.display, fontWeight: 600, fontSize: 20, color: left < 0 ? 'error.main' : 'text.primary' }}>
                  <CountUp from={0} value={left} delay={k * 0.1} duration={1} />
                </Typography>
                {of > 0 && <Typography variant="caption" color="text.secondary">of {of}</Typography>}
              </Box>
            </Box>
            <Typography variant="body2" sx={{ fontWeight: 700, maxWidth: '100%' }} noWrap>{labelOf(mem.name)}</Typography>
            {/* hourly: the figure is days taken this year, not days left (#300). */}
            <Typography variant="caption" color="text.secondary">{bc ? 'vacation taken' : 'vacation left'}</Typography>
          </m.div>
        );
      })}
      </AnimatePresence>
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
          transformOrigin: 'left', animation: `${KF.growX} .6s ease-out .2s both`,
          // While its card is hovered the stripes drift, to say "this part would change". Still at rest.
          // Both animations stay listed, so leaving the card never replays the grow.
          'article:hover &': { animation: `${KF.growX} .6s ease-out .2s both, ${KF.stripes} .9s linear infinite` },
        }}
      />
    </Box>
  );
}
