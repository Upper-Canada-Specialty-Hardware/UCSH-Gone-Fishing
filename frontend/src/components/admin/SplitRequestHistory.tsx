import { ComponentProps } from 'react';
import { Box, Typography } from '@mui/material';
import RequestHistory from '../RequestHistory';
import { isHourly } from '../../utils/employment';

/** The props RequestHistory takes; the wrapper passes them straight through. */
type RequestHistoryProps = ComponentProps<typeof RequestHistory>;

/**
 * Show salaried and hourly team requests in two separate {@link RequestHistory}
 * tables, each under its own heading (#338). Each table keeps its own columns,
 * filters and sorting. Rows split by the request's recorded employment type when
 * the backend sent one, else by the person's current type; a row with no type
 * counts as salaried. When only one type is present there is nothing to
 * separate, so a single table is shown.
 *
 * @param props - The same props RequestHistory takes.
 * @returns One table, or the salaried and hourly tables stacked.
 */
export default function SplitRequestHistory(props: RequestHistoryProps) {
  const requests = props.requests || [];
  const hourly = requests.filter((r: any) => isHourly(r?.employment_type));     // "Hourly"/"Hourly BC"
  const salaried = requests.filter((r: any) => !isHourly(r?.employment_type));  // Salary, or unknown

  // Nothing to separate when one side is empty (a single-type team, or no rows).
  if (hourly.length === 0 || salaried.length === 0) {
    return <RequestHistory {...props} />;
  }

  const sections: [string, any[]][] = [['Salaried', salaried], ['Hourly', hourly]];
  return (
    <Box sx={{ display: 'grid', gap: 2.5 }}>
      {sections.map(([label, group]) => (
        <Box key={label}>
          <Typography variant="subtitle2" sx={{ mb: 1, fontWeight: 700 }}>
            {label} · {group.length} {group.length === 1 ? 'request' : 'requests'}
          </Typography>
          <RequestHistory {...props} requests={group} />
        </Box>
      ))}
    </Box>
  );
}
