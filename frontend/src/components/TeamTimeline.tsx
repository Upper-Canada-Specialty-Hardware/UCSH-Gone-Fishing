import { useMemo, useState } from 'react';
import { Box, Typography, IconButton, Button, Tooltip } from '@mui/material';
import ChevronLeftIcon from '@mui/icons-material/ChevronLeft';
import ChevronRightIcon from '@mui/icons-material/ChevronRight';

interface CalendarEvent {
  id: string;
  employee: string;
  start: string;
  end: string;
  leave_type: string;
  days: string | number;
}

interface Props {
  events: CalendarEvent[];
}

const LEAVE_COLORS: Record<string, string> = {
  Vacation: '#2563eb',
  Sick: '#dc2626',
  Personal: '#7c3aed',
  Bereavement: '#6b7280',
};
const FALLBACK_COLOR = '#0891b2';

function getMonday(d: Date): Date {
  const date = new Date(d);
  const day = date.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  date.setDate(date.getDate() + diff);
  date.setHours(0, 0, 0, 0);
  return date;
}

function addDays(d: Date, n: number): Date {
  const date = new Date(d);
  date.setDate(date.getDate() + n);
  return date;
}

function formatDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function formatLabel(d: Date): string {
  const weekday = d.toLocaleDateString('en-US', { weekday: 'short' });
  return `${weekday} ${d.getDate()}`;
}

function formatMonthRange(start: Date, end: Date): string {
  const sMonth = start.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
  const eMonth = end.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
  if (sMonth === eMonth) return sMonth;
  return `${start.toLocaleDateString('en-US', { month: 'short' })} – ${eMonth}`;
}

export default function TeamTimeline({ events }: Props) {
  const [rangeStart, setRangeStart] = useState(() => getMonday(new Date()));
  const totalDays = 14;

  const dates = useMemo(() => {
    const arr: Date[] = [];
    for (let i = 0; i < totalDays; i++) arr.push(addDays(rangeStart, i));
    return arr;
  }, [rangeStart]);

  const rangeEnd = addDays(rangeStart, totalDays - 1);
  const rangeStartStr = formatDate(rangeStart);
  const rangeEndStr = formatDate(rangeEnd);

  const filteredEvents = useMemo(
    () => events.filter((e) => e.start <= rangeEndStr && (e.end || e.start) >= rangeStartStr),
    [events, rangeStartStr, rangeEndStr],
  );

  const employees = useMemo(() => {
    const names = [...new Set(filteredEvents.map((e) => e.employee))];
    names.sort((a, b) => a.localeCompare(b));
    return names;
  }, [filteredEvents]);

  const leaveTypesUsed = useMemo(
    () => [...new Set(filteredEvents.map((e) => e.leave_type))].sort(),
    [filteredEvents],
  );

  const prev = () => setRangeStart((s) => addDays(s, -14));
  const next = () => setRangeStart((s) => addDays(s, 14));
  const today = () => setRangeStart(getMonday(new Date()));

  if (events.length === 0) {
    return (
      <Typography color="text.secondary" sx={{ py: 4, textAlign: 'center' }}>
        No approved leave to display.
      </Typography>
    );
  }

  return (
    <Box>
      {/* Navigation */}
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <IconButton onClick={prev} size="small"><ChevronLeftIcon /></IconButton>
          <Button onClick={today} size="small" variant="outlined">Today</Button>
          <IconButton onClick={next} size="small"><ChevronRightIcon /></IconButton>
        </Box>
        <Typography variant="subtitle1" fontWeight={600}>
          {formatMonthRange(rangeStart, rangeEnd)}
        </Typography>
      </Box>

      {filteredEvents.length === 0 ? (
        <Typography color="text.secondary" sx={{ py: 4, textAlign: 'center' }}>
          No leave events in this date range.
        </Typography>
      ) : (
        <Box sx={{ overflowX: 'auto' }}>
          <Box
            sx={{
              display: 'grid',
              gridTemplateColumns: `160px repeat(${totalDays}, minmax(48px, 1fr))`,
              minWidth: 160 + totalDays * 48,
            }}
          >
            {/* Header row */}
            <Box sx={{ borderBottom: '2px solid', borderColor: 'divider', p: 0.5 }} />
            {dates.map((d) => {
              const isWeekend = d.getDay() === 0 || d.getDay() === 6;
              const isToday = formatDate(d) === formatDate(new Date());
              return (
                <Box
                  key={formatDate(d)}
                  sx={{
                    borderBottom: '2px solid',
                    borderColor: 'divider',
                    p: 0.5,
                    textAlign: 'center',
                    fontSize: '0.75rem',
                    fontWeight: isToday ? 700 : 400,
                    bgcolor: isWeekend ? 'action.hover' : isToday ? 'primary.50' : undefined,
                    color: isToday ? 'primary.main' : undefined,
                  }}
                >
                  {formatLabel(d)}
                </Box>
              );
            })}

            {/* Employee rows */}
            {employees.map((emp) => {
              const empEvents = filteredEvents.filter((e) => e.employee === emp);
              return (
                <Box key={emp} sx={{ display: 'contents' }}>
                  {/* Name cell */}
                  <Box
                    sx={{
                      p: 1,
                      fontSize: '0.8125rem',
                      fontWeight: 500,
                      borderBottom: '1px solid',
                      borderColor: 'divider',
                      display: 'flex',
                      alignItems: 'center',
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {emp}
                  </Box>

                  {/* Day cells + bars */}
                  <Box
                    sx={{
                      gridColumn: `2 / ${totalDays + 2}`,
                      display: 'grid',
                      gridTemplateColumns: `repeat(${totalDays}, minmax(48px, 1fr))`,
                      position: 'relative',
                      borderBottom: '1px solid',
                      borderColor: 'divider',
                      minHeight: 36,
                    }}
                  >
                    {/* Background day cells */}
                    {dates.map((d) => {
                      const isWeekend = d.getDay() === 0 || d.getDay() === 6;
                      return (
                        <Box
                          key={formatDate(d)}
                          sx={{
                            borderRight: '1px solid',
                            borderColor: 'divider',
                            bgcolor: isWeekend ? 'action.hover' : undefined,
                          }}
                        />
                      );
                    })}

                    {/* Event bars */}
                    {empEvents.map((ev) => {
                      const evStart = ev.start < rangeStartStr ? rangeStartStr : ev.start;
                      const evEnd = (ev.end || ev.start) > rangeEndStr ? rangeEndStr : (ev.end || ev.start);

                      const startIdx = dates.findIndex((d) => formatDate(d) === evStart);
                      const endIdx = dates.findIndex((d) => formatDate(d) === evEnd);
                      if (startIdx === -1 || endIdx === -1) return null;

                      const colStart = startIdx + 1;
                      const colEnd = endIdx + 2;
                      const span = colEnd - colStart;
                      const color = LEAVE_COLORS[ev.leave_type] || FALLBACK_COLOR;

                      return (
                        <Tooltip
                          key={ev.id}
                          title={`${ev.employee} — ${ev.leave_type}\n${ev.start} to ${ev.end || ev.start} (${ev.days} day${ev.days === 1 ? '' : 's'})`}
                          arrow
                        >
                          <Box
                            sx={{
                              position: 'absolute',
                              top: 4,
                              bottom: 4,
                              gridColumn: `${colStart} / ${colEnd}`,
                              left: `calc(${((colStart - 1) / totalDays) * 100}% + 2px)`,
                              width: `calc(${(span / totalDays) * 100}% - 4px)`,
                              bgcolor: color,
                              borderRadius: '4px',
                              color: 'white',
                              fontSize: '0.6875rem',
                              fontWeight: 500,
                              display: 'flex',
                              alignItems: 'center',
                              px: 0.5,
                              overflow: 'hidden',
                              whiteSpace: 'nowrap',
                              cursor: 'default',
                            }}
                          >
                            {span >= 2 ? ev.leave_type : ''}
                          </Box>
                        </Tooltip>
                      );
                    })}
                  </Box>
                </Box>
              );
            })}
          </Box>
        </Box>
      )}

      {/* Legend */}
      <Box sx={{ display: 'flex', gap: 2, mt: 2, flexWrap: 'wrap' }}>
        {leaveTypesUsed.map((type) => (
          <Box key={type} sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
            <Box
              sx={{
                width: 12,
                height: 12,
                borderRadius: '2px',
                bgcolor: LEAVE_COLORS[type] || FALLBACK_COLOR,
              }}
            />
            <Typography variant="caption">{type}</Typography>
          </Box>
        ))}
      </Box>
    </Box>
  );
}
