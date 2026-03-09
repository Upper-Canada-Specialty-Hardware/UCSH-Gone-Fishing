import { useCallback, useMemo } from 'react';
import { Calendar, dateFnsLocalizer, Event } from 'react-big-calendar';
import { Box, Typography } from '@mui/material';
import 'react-big-calendar/lib/css/react-big-calendar.css';

// Minimal date-fns replacement using native Date
const localizer = dateFnsLocalizer({
  format: (date: Date, formatStr: string) => {
    if (formatStr === 'MMMM yyyy') return date.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
    if (formatStr === 'dd') return date.getDate().toString().padStart(2, '0');
    if (formatStr === 'EEE') return date.toLocaleDateString('en-US', { weekday: 'short' });
    if (formatStr === 'hh:mm a') return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
    return date.toLocaleDateString();
  },
  parse: (value: string) => new Date(value),
  startOfWeek: () => 0,
  getDay: (date: Date) => date.getDay(),
  locales: { 'en-US': {} },
});

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

const COLORS = [
  '#2563eb', '#16a34a', '#dc2626', '#7c3aed', '#ea580c',
  '#0891b2', '#be185d', '#65a30d', '#4f46e5', '#0d9488',
];

export default function TeamCalendar({ events }: Props) {
  const employeeColors = useMemo(() => {
    const map: Record<string, string> = {};
    const names = [...new Set(events.map((e) => e.employee))];
    names.forEach((name, i) => {
      map[name] = COLORS[i % COLORS.length];
    });
    return map;
  }, [events]);

  const calendarEvents: Event[] = useMemo(
    () =>
      events.map((e) => ({
        title: `${e.employee} — ${e.leave_type}`,
        start: new Date(e.start + 'T00:00:00'),
        end: new Date((e.end || e.start) + 'T23:59:59'),
        allDay: true,
        resource: e,
      })),
    [events]
  );

  const eventStyleGetter = useCallback(
    (event: Event) => {
      const emp = (event.resource as CalendarEvent)?.employee || '';
      return {
        style: {
          backgroundColor: employeeColors[emp] || '#6b7280',
          borderRadius: '4px',
          border: 'none',
          color: 'white',
          fontSize: '12px',
        },
      };
    },
    [employeeColors]
  );

  if (events.length === 0) {
    return (
      <Typography color="text.secondary" sx={{ py: 4, textAlign: 'center' }}>
        No approved leave to display.
      </Typography>
    );
  }

  return (
    <Box sx={{ height: 600 }}>
      <Calendar
        localizer={localizer}
        events={calendarEvents}
        startAccessor="start"
        endAccessor="end"
        views={['month', 'week']}
        defaultView="month"
        eventPropGetter={eventStyleGetter}
        popup
      />
    </Box>
  );
}
