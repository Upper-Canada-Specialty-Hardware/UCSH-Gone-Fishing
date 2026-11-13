import { DataGrid, GridColDef } from '@mui/x-data-grid';
import { Box } from '@mui/material';
import { SHARED_DATA_GRID_PROPS } from './dataGridDefaults';
import { vacationLabels } from '../utils/vacationLines';

interface Props {
  members: any[];
  loading?: boolean;
}

export default function TeamBalanceTable({ members, loading }: Props) {
  const columns: GridColDef[] = [
    { field: 'name', headerName: 'Employee', width: 200 },
    { field: 'department', headerName: 'Department', width: 140 },
    { field: 'location', headerName: 'Location', width: 160 },
    {
      field: 'vacation',
      headerName: vacationLabels().current,   // the current-year line (#551)
      width: 100,
      type: 'number',
      valueGetter: (_: any, row: any) => row.balances?.vacation_balance ?? 0,
      cellClassName: (params) => (params.value as number) < 0 ? 'cell-negative' : '',
    },
    {
      field: 'sick',
      headerName: 'Sick',
      width: 80,
      type: 'number',
      valueGetter: (_: any, row: any) => row.balances?.sick_balance ?? 0,
      cellClassName: (params) => (params.value as number) < 0 ? 'cell-negative' : '',
    },
    {
      field: 'overtime',
      headerName: 'Make-Up',
      width: 100,
      type: 'number',
      valueGetter: (_: any, row: any) => row.balances?.overtime ?? 0,
    },
    {
      field: 'carryover',
      headerName: vacationLabels().next,      // the next-year line, named by its dates
      width: 100,
      type: 'number',
      valueGetter: (_: any, row: any) => row.balances?.carryover ?? 0,
    },
  ];

  const rows = members.map((m: any) => ({
    id: m.id,
    name: m.name || m.employee?.name,
    department: m.department || m.employee?.department,
    location: m.location || m.employee?.location,
    balances: m.balances,
  }));

  return (
    <Box
      sx={{
        '& .cell-negative': { color: 'error.main', fontWeight: 600 },  // below zero
      }}
    >
      <DataGrid
        rows={rows}
        columns={columns}
        loading={loading}
        {...SHARED_DATA_GRID_PROPS}
      />
    </Box>
  );
}
