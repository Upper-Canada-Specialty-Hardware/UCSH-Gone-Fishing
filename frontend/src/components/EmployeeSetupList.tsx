import { useMemo } from 'react';
import {
  Box, Alert, Chip, Stack, Button, Typography, CircularProgress,
} from '@mui/material';
import { DataGrid, GridColDef, GridActionsCellItem, GridActionsCellItemProps } from '@mui/x-data-grid';
import RefreshIcon from '@mui/icons-material/Refresh';
import FactCheckIcon from '@mui/icons-material/FactCheck';
import { SHARED_DATA_GRID_PROPS } from './dataGridDefaults';
import { PROBLEM_INFO } from './employeeSetupProblems';

// One failing check, as the sweep reports it.
interface Problem {
  code: string;
  category: string;
  detail: string;
}

export interface EmployeeSetupSummary {
  flagged: {
    employee_id: string;
    employee_name: string;
    department: string;
    location: string;
    fails: Problem[];
    warns: Problem[];
  }[];
  total_checked: number;
  directory_unreadable: boolean;
}

interface Props {
  setupList: EmployeeSetupSummary | null;
  loading: boolean;
  onRefresh: () => Promise<void>;
  onSelect: (employeeId: string) => void;
}

/**
 * Every Staff Directory record whose setup would stall a request, listed up
 * front rather than found one at a time.
 *
 * The per-employee check answers the same question on demand, which means a
 * broken record is only ever looked at once someone has already been blocked by
 * it. This is the same set of checks run across everyone.
 */
export default function EmployeeSetupList({ setupList, loading, onRefresh, onSelect }: Props) {
  const rows = useMemo(
    () =>
      (setupList?.flagged || []).map((r) => ({
        id: r.employee_id,
        employee_name: r.employee_name || `#${r.employee_id}`,
        department: r.department || '',
        location: r.location || '',
        fails: r.fails || [],
      })),
    [setupList],
  );

  // Problems sit right after the name: the reason a record is listed is the
  // first thing to read, and on a narrow viewport, where the grid scrolls
  // sideways, it is the column that must still be on screen.
  const columns = useMemo<GridColDef[]>(() => [
    { field: 'employee_name', headerName: 'Employee', width: 190 },
    {
      field: 'fails',
      headerName: 'Problems',
      flex: 1,
      minWidth: 260,
      sortable: false,
      filterable: false,
      renderCell: (params) => (
        <Stack direction="row" spacing={0.5} flexWrap="wrap" alignItems="center" sx={{ py: 0.5 }}>
          {(params.value as Problem[]).map((problem) => (
            <Chip
              key={problem.code}
              label={PROBLEM_INFO[problem.code]?.title || problem.code}
              color="error"
              size="small"
              // Titles run long ("A supervisor's email does not match a
              // Microsoft 365 account"); on a narrow viewport the label wraps
              // inside the column instead of being cut off at its edge.
              sx={{ height: 'auto', py: 0.25, '& .MuiChip-label': { whiteSpace: 'normal' } }}
            />
          ))}
        </Stack>
      ),
    },
    { field: 'location', headerName: 'Location', width: 150 },
    { field: 'department', headerName: 'Department', width: 150 },
    {
      field: 'actions',
      headerName: 'Actions',
      type: 'actions',
      width: 100,
      getActions: (params) => {
        const actions: React.ReactElement<GridActionsCellItemProps>[] = [
          <GridActionsCellItem
            key="check"
            icon={<FactCheckIcon color="primary" />}
            label="Check setup"
            onClick={() => onSelect(String(params.id))}
          />,
        ];
        return actions;
      },
    },
  ], [onSelect]);

  return (
    <Box sx={{ mb: 3 }}>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 2, mb: 1.5 }}>
        <Typography variant="body2" color="text.secondary">
          A request from anyone listed here would stall. Fix their Staff Directory record, then
          Refresh to confirm it clears.
        </Typography>
        <Button
          size="small"
          variant="outlined"
          onClick={onRefresh}
          disabled={loading}
          startIcon={loading ? <CircularProgress size={14} color="inherit" /> : <RefreshIcon />}
          sx={{ flexShrink: 0 }}
        >
          Refresh
        </Button>
      </Box>

      {setupList?.directory_unreadable && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          The Microsoft 365 directory could not be read, so the identity checks were skipped. Refresh
          to run them again.
        </Alert>
      )}

      {setupList && rows.length === 0 ? (
        <Alert severity="success">
          All {setupList.total_checked} Staff Directory records pass the setup checks.
        </Alert>
      ) : (
        <DataGrid
          rows={rows}
          columns={columns}
          loading={loading}
          {...SHARED_DATA_GRID_PROPS}
          // A record can carry several problems. The row grows to show every
          // chip rather than clipping the second line at the fixed height.
          getRowHeight={() => 'auto'}
          sx={{ '& .MuiDataGrid-cell': { minHeight: 52, py: 0.5, display: 'flex', alignItems: 'center' } }}
        />
      )}
    </Box>
  );
}
