import { useMemo, useState } from 'react';
import { Box, Button, ButtonBase, Tooltip, Typography } from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import { kindColor } from '../../constants/leaveColors';
import PillToggle from '../PillToggle';
import { Row } from './requestTable';

/**
 * The requests calendar (#280): the same filtered leave requests the table
 * shows, laid out by date like the Leave Calendar. A month grid with each
 * person's time off, and a Year view of twelve mini-months shaded by how many
 * people are off. The month name opens a picker to jump to any month of any
 * year or type a date. Only leave has a date range, so make-up and carry-over
 * (which have no span) are left to the table.
 */

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/** YYYY-MM-DD for a date, read and written in local parts (no timezone shift). */
function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** A YYYY-MM-DD string to a Date at local midnight. */
function parse(s: string): Date {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
}

/** Monday-to-Friday only; weekends never count as days off. */
function isWeekday(d: Date): boolean {
  return d.getDay() !== 0 && d.getDay() !== 6;
}

/** "Oct 14" / "Oct 14, 2027" (the year only when not the month's own). */
function nice(s: string, withYear = false): string {
  if (!s) return '';
  const d = parse(s);
  const mon = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getMonth()];
  return `${mon} ${d.getDate()}${withYear ? `, ${d.getFullYear()}` : ''}`;
}

/** The person's first name and last initial, so an event bar fits. */
function shortName(name: string): string {
  const parts = name.split(' ').filter(Boolean);
  return parts.length > 1 ? `${parts[0]} ${parts[1][0]}.` : parts[0] || name;
}

interface Props {
  /** The leave rows in scope (already filtered by view, source, search and filters). */
  rows: Row[];
  /** Holiday date (YYYY-MM-DD) to its name; empty when not loaded. */
  holidays?: Record<string, string>;
  /** Open a request in the table (clicking an event). */
  onOpenRequest: (row: Row) => void;
}

/**
 * The calendar view of the requests table.
 *
 * @param props - See {@link Props}.
 * @returns The calendar.
 */
export default function RequestsCalendar({ rows, holidays = {}, onOpenRequest }: Props) {
  const theme = useTheme();
  const mode = theme.palette.mode;
  const today = new Date();
  const [month, setMonth] = useState(() => new Date(today.getFullYear(), today.getMonth(), 1));
  const [view, setView] = useState<'month' | 'year'>('month');
  const [picking, setPicking] = useState(false);
  const [pickYear, setPickYear] = useState(today.getFullYear());

  // Only dated leave sits on the calendar; make-up and carry-over have no span.
  const leave = useMemo(() => rows.filter((r) => r.src === 'leave' && r.start), [rows]);

  /** The leave rows covering one day (weekday only). @param d - The day. @returns Rows off that day. */
  const offOn = (d: Date): Row[] => {
    if (!isWeekday(d)) return [];
    const s = iso(d);
    return leave.filter((r) => s >= r.start && s <= (r.end || r.start));
  };

  /** Step a month (month view) or a year (year view). @param dir - -1 back, 1 forward. */
  const step = (dir: number) => setMonth((m) => (view === 'year' ? new Date(m.getFullYear() + dir, m.getMonth(), 1) : new Date(m.getFullYear(), m.getMonth() + dir, 1)));

  /** Jump to a day's month. @param d - The day. */
  const goTo = (d: Date) => { setMonth(new Date(d.getFullYear(), d.getMonth(), 1)); setView('month'); setPicking(false); };

  const line = theme.tokens.line;

  // The month grid: 6 weeks from the Monday on or before the 1st.
  const monthCells = useMemo(() => {
    const start = new Date(month);
    start.setDate(1 - ((month.getDay() + 6) % 7));                // back to Monday
    const cells: Date[] = [];
    for (let i = 0; i < 42; i++) {
      const d = new Date(start); d.setDate(start.getDate() + i);
      if (i >= 35 && d.getMonth() !== month.getMonth()) break;    // drop a trailing empty week
      cells.push(d);
    }
    return cells;
  }, [month]);

  return (
    <Box sx={{ p: 1.75, display: 'grid', gap: 1.25 }}>
      {/* Header: the month name opens the picker; Month/Year; Today; step arrows. */}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1.25 }}>
        <ButtonBase
          onClick={() => { setPicking((p) => !p); setPickYear(month.getFullYear()); }}
          aria-expanded={picking}
          sx={{ fontFamily: 'Fraunces, Georgia, serif', fontSize: 22, fontWeight: 600, px: 1, py: 0.25, ml: -1, borderRadius: 2.5, flex: '1 1 auto', justifyContent: 'flex-start', gap: 0.75, '&:hover': { bgcolor: theme.tokens.hover } }}
        >
          {view === 'year' ? month.getFullYear() : `${MONTHS[month.getMonth()]} ${month.getFullYear()}`}
          <Box component="span" sx={{ fontSize: 13, color: 'text.secondary', transition: 'transform .25s', transform: picking ? 'rotate(180deg)' : 'none' }}>▾</Box>
        </ButtonBase>
        <PillToggle name="cal-view" label="Calendar view" value={view} onChange={(v) => { setView(v); setPicking(false); }} options={[{ key: 'month', label: 'Month' }, { key: 'year', label: 'Year' }]} />
        <Button size="small" variant="outlined" color="inherit" onClick={() => { setMonth(new Date(today.getFullYear(), today.getMonth(), 1)); setView('month'); setPicking(false); }}>Today</Button>
        <Button size="small" variant="outlined" color="inherit" onClick={() => step(-1)} aria-label={view === 'year' ? 'Previous year' : 'Previous month'} sx={{ minWidth: 36 }}>‹</Button>
        <Button size="small" variant="outlined" color="inherit" onClick={() => step(1)} aria-label={view === 'year' ? 'Next year' : 'Next month'} sx={{ minWidth: 36 }}>›</Button>
      </Box>

      {/* The jump picker: a year stepper and the twelve months with their counts. */}
      {picking && (
        <Box sx={{ border: `1px solid ${theme.tokens.line2}`, borderRadius: 3.5, p: 1.5, bgcolor: 'background.paper', display: 'grid', gap: 1.5 }}>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1.25 }}>
            <Button size="small" variant="outlined" color="inherit" onClick={() => setPickYear((y) => y - 1)} sx={{ minWidth: 36 }}>‹</Button>
            <b style={{ fontSize: 18, minWidth: 48, textAlign: 'center' }}>{pickYear}</b>
            <Button size="small" variant="outlined" color="inherit" onClick={() => setPickYear((y) => y + 1)} sx={{ minWidth: 36 }}>›</Button>
            <Button size="small" onClick={() => { setMonth(new Date(pickYear, month.getMonth(), 1)); setView('year'); setPicking(false); }}>See all of {pickYear}</Button>
            <Box component="label" sx={{ ml: 'auto', fontSize: 13, color: 'text.secondary', display: 'inline-flex', alignItems: 'center', gap: 1 }}>
              Go to a date
              <input type="date" min="2024-01-01" max="2030-12-31" defaultValue={iso(today)} onChange={(e) => { if (e.target.value) goTo(parse(e.target.value)); }} style={{ font: 'inherit', padding: '6px 10px', borderRadius: 10, border: `1px solid ${theme.tokens.line2}`, background: theme.tokens.bg, color: theme.palette.text.primary }} />
            </Box>
          </Box>
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(96px, 1fr))', gap: 0.75 }}>
            {MONTHS.map((name, i) => {
              const first = iso(new Date(pickYear, i, 1));
              const last = iso(new Date(pickYear, i + 1, 0));
              const n = leave.filter((r) => r.start <= last && (r.end || r.start) >= first).length;
              const on = pickYear === month.getFullYear() && i === month.getMonth();
              return (
                <ButtonBase key={name} onClick={() => goTo(new Date(pickYear, i, 1))} sx={{ border: `1px solid ${on ? theme.palette.primary.main : line}`, bgcolor: on ? 'primary.main' : theme.tokens.bg, color: on ? 'primary.contrastText' : 'text.primary', borderRadius: 2.5, px: 1.25, py: 1, display: 'grid', textAlign: 'left', justifyItems: 'start' }}>
                  <b style={{ fontSize: 13.5 }}>{name.slice(0, 3)}</b>
                  <Box component="small" sx={{ fontSize: 11.5, opacity: on ? 0.85 : 1, color: on ? 'inherit' : 'text.secondary' }}>{n ? `${n} request${n === 1 ? '' : 's'}` : 'none'}</Box>
                </ButtonBase>
              );
            })}
          </Box>
        </Box>
      )}

      {/* The colour key and a note on what the marks mean. */}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: '6px 14px', alignItems: 'center', fontSize: 12.5, color: 'text.secondary' }}>
        {view === 'month'
          ? 'Dashed outline: pending. Struck through: rejected or refunded. Click a request to open it.'
          : 'Darker days have more people off. Click a day or a month to open it.'}
      </Box>

      {view === 'month' ? (
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))', border: `1px solid ${line}`, borderRadius: 3, overflow: 'hidden' }}>
          {DOW.map((d) => (
            <Box key={d} sx={{ fontSize: 11.5, fontWeight: 700, color: 'text.secondary', p: '6px 8px', bgcolor: theme.tokens.hover, borderBottom: `1px solid ${line}` }}>{d}</Box>
          ))}
          {monthCells.map((d, i) => {
            const outside = d.getMonth() !== month.getMonth();
            const isToday = iso(d) === iso(today);
            const on = offOn(d);
            const hol = holidays[iso(d)];
            return (
              <Box key={i} sx={{ minHeight: { xs: 70, sm: 104 }, p: '4px 4px 6px', borderRight: `1px solid ${line}`, borderBottom: `1px solid ${line}`, display: 'grid', alignContent: 'start', gap: '3px', minWidth: 0, bgcolor: outside ? theme.tokens.bg : !isWeekday(d) ? theme.tokens.hover : 'transparent', color: outside ? 'text.secondary' : 'text.primary', '&:nth-of-type(7n)': { borderRight: 0 } }}>
                <Box sx={{ fontSize: 12, fontWeight: 700, px: 0.5, display: 'flex', justifyContent: 'space-between', gap: 0.5 }}>
                  <Box component="span" sx={isToday ? { bgcolor: 'secondary.main', color: 'background.paper', borderRadius: 999, px: 0.75 } : undefined}>{d.getDate()}</Box>
                </Box>
                {hol && <Box sx={{ fontSize: 10.5, fontWeight: 700, color: 'error.main', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{hol}</Box>}
                {on.slice(0, 3).map((r) => {
                  const c = kindColor(r.kind, mode);
                  const rej = r.status === 'Rejected' || r.status === 'Refunded';
                  return (
                    <Tooltip key={r.key} title={`${r.person}, ${r.kindLabel}, ${nice(r.start)}${r.end && r.end !== r.start ? ` to ${nice(r.end)}` : ''} (${r.status})`}>
                      <ButtonBase onClick={() => onOpenRequest(r)} sx={{ fontSize: 11.5, fontWeight: 600, px: 0.75, py: '1px', borderRadius: 1, justifyContent: 'flex-start', color: c, bgcolor: alpha(c, mode === 'dark' ? 0.18 : 0.12), outline: r.status === 'Pending' ? `1.5px dashed ${c}` : 'none', outlineOffset: '-1.5px', textDecoration: rej ? 'line-through' : 'none', opacity: rej ? 0.6 : 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block', textAlign: 'left' }}>
                        {shortName(r.person)}
                      </ButtonBase>
                    </Tooltip>
                  );
                })}
                {on.length > 3 && <Box sx={{ fontSize: 11, color: 'text.secondary', px: 0.75 }}>+{on.length - 3} more</Box>}
              </Box>
            );
          })}
        </Box>
      ) : (
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 1.5 }}>
          {Array.from({ length: 12 }, (_, mi) => {
            const y = month.getFullYear();
            const first = new Date(y, mi, 1);
            const lastIso = iso(new Date(y, mi + 1, 0));
            const inMonth = leave.filter((r) => r.start <= lastIso && (r.end || r.start) >= iso(first));
            let daysOff = 0;
            inMonth.forEach((r) => {
              for (const d = parse(r.start > iso(first) ? r.start : iso(first)); iso(d) <= (r.end || r.start) && iso(d) <= lastIso; d.setDate(d.getDate() + 1)) {
                if (isWeekday(d) && r.status !== 'Rejected' && r.status !== 'Refunded') daysOff++;
              }
            });
            const lead = (first.getDay() + 6) % 7;
            const daysInMonth = new Date(y, mi + 1, 0).getDate();
            return (
              <Box key={mi} sx={{ border: `1px solid ${line}`, borderRadius: 3, p: 1.25, display: 'grid', gap: 0.5, alignContent: 'start' }}>
                <ButtonBase onClick={() => goTo(first)} sx={{ fontFamily: 'Fraunces, Georgia, serif', fontSize: 16, fontWeight: 600, justifyContent: 'flex-start', '&:hover': { color: 'primary.main', textDecoration: 'underline' } }}>{MONTHS[mi]}</ButtonBase>
                <Box sx={{ fontSize: 12, color: 'text.secondary' }}>{inMonth.length ? `${inMonth.length} request${inMonth.length === 1 ? '' : 's'} · ${daysOff} day${daysOff === 1 ? '' : 's'} off` : 'Nobody off'}</Box>
                <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))', gap: '2px', mt: 0.5 }}>
                  {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((x, k) => <Box key={k} sx={{ fontSize: 10, color: 'text.secondary', textAlign: 'center', fontWeight: 700 }}>{x}</Box>)}
                  {Array.from({ length: lead }, (_, k) => <span key={`b${k}`} />)}
                  {Array.from({ length: daysInMonth }, (_, k) => {
                    const dd = k + 1;
                    const d = new Date(y, mi, dd);
                    const on = offOn(d).filter((r) => r.status !== 'Rejected' && r.status !== 'Refunded');
                    const heat = Math.min(3, on.length);
                    const teal = kindColor('vacation', mode);
                    const bg = heat === 0 ? 'transparent' : heat === 1 ? alpha(teal, 0.18) : heat === 2 ? alpha(teal, 0.4) : teal;
                    const isToday = iso(d) === iso(today);
                    return (
                      <Tooltip key={dd} title={`${nice(iso(d), true)}${on.length ? `: ${on.map((r) => r.person).join(', ')}` : ': nobody off'}`}>
                        <ButtonBase onClick={() => goTo(d)} sx={{ aspectRatio: '1', borderRadius: 1.5, fontSize: 11, bgcolor: bg, color: heat === 3 ? 'background.paper' : isWeekday(d) ? 'text.primary' : 'text.secondary', outline: isToday ? `2px solid ${theme.palette.secondary.main}` : 'none', outlineOffset: '-1px', fontWeight: isToday ? 700 : 400 }}>{dd}</ButtonBase>
                      </Tooltip>
                    );
                  })}
                </Box>
              </Box>
            );
          })}
        </Box>
      )}

      {leave.length === 0 && <Typography color="text.secondary" sx={{ py: 2, textAlign: 'center' }}>No dated leave to show for these filters.</Typography>}
    </Box>
  );
}
