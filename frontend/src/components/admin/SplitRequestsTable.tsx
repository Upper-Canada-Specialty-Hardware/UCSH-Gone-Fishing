import { ComponentProps, useCallback } from 'react';
import { Box } from '@mui/material';
import RequestsTable from './RequestsTable';
import { Row } from './requestTable';
import { isHourly } from '../../utils/employment';

/** The props RequestsTable takes; the wrapper passes them straight through. */
type RequestsTableProps = ComponentProps<typeof RequestsTable>;

/**
 * Show salaried and hourly requests in two separate tables, each under its own
 * heading (#338). Each table is the existing {@link RequestsTable} with one
 * group's rows, so sorting, grouping, selection (one shared set), the inline
 * actions and the no-sideways-scroll fit all keep working. Rows split by the
 * request's recorded employment type when the backend sent one, else by the
 * person's current type; a row with no type counts as salaried. When only one
 * type is present there is nothing to separate, so a single table is shown.
 *
 * @param props - The same props RequestsTable takes.
 * @returns One table, or the salaried and hourly tables stacked.
 */
export default function SplitRequestsTable(props: RequestsTableProps) {
  const { rows, onFitNote } = props;
  // A stable no-op so only the first table reports dropped columns to the footer.
  const noFitNote = useCallback(() => {}, []);

  const hourly = rows.filter((r) => isHourly(r.employment_type));     // "Hourly" or "Hourly BC"
  const salaried = rows.filter((r) => !isHourly(r.employment_type));  // Salary, or unknown type

  // Nothing to separate when one side is empty (a single-type view, or no rows).
  if (hourly.length === 0 || salaried.length === 0) {
    return <RequestsTable {...props} />;
  }

  const sections: [string, Row[]][] = [['Salaried', salaried], ['Hourly', hourly]];
  return (
    <Box>
      {sections.map(([label, group], index) => (
        <Box key={label}>
          <Heading label={label} count={group.length} />
          <RequestsTable
            {...props}
            rows={group}
            onFitNote={index === 0 ? onFitNote : noFitNote}   // one owner of the footer note
          />
        </Box>
      ))}
    </Box>
  );
}

/**
 * The bar above one table naming its group and how many requests it holds.
 *
 * @param props.label - "Salaried" or "Hourly".
 * @param props.count - How many requests are in the group.
 * @returns The heading bar.
 */
function Heading({ label, count }: { label: string; count: number }) {
  return (
    <Box sx={{ px: 1.75, py: 1, fontSize: 13.5, fontWeight: 700, color: 'text.secondary', bgcolor: 'action.hover', borderBottom: 1, borderColor: 'divider' }}>
      {label} · {count} {count === 1 ? 'request' : 'requests'}
    </Box>
  );
}
