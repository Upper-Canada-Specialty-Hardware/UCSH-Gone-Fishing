import { useCallback, useMemo } from 'react';
import { Calendar, dateFnsLocalizer, Event } from 'react-big-calendar';
import { Box, Typography } from '@mui/material';
import 'react-big-calendar/lib/css/react-big-calendar.css';
import { LEAVE_COLORS, FALLBACK_COLOR } from '../constants/leaveColors';

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
  startOfWeek: (date: Date) => {
    const d = new Date(date);
    d.setDate(d.getDate() - d.getDay());
    d.setHours(0, 0, 0, 0);
    return d;
  },
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

export default function TeamCalendar({ events }: Props) {
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
      const leaveType = (event.resource as CalendarEvent)?.leave_type || '';
      return {
        style: {
          backgroundColor: LEAVE_COLORS[leaveType] || FALLBACK_COLOR,
          borderRadius: '4px',
          border: 'none',
          color: 'white',
          fontSize: '12px',
        },
      };
    },
    []
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
        views={['month']}
        defaultView="month"
        eventPropGetter={eventStyleGetter}
        popup
      />
    </Box>
  );
}
