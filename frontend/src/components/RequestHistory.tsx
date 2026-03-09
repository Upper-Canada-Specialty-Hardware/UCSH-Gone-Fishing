import { useState } from 'react';
import { DataGrid, GridColDef } from '@mui/x-data-grid';
import { Box, Chip, FormControl, InputLabel, Select, MenuItem } from '@mui/material';

interface Props {
  requests: any[];
  loading?: boolean;
  showEmployee?: boolean;
}

const statusColor: Record<string, 'success' | 'error' | 'warning' | 'default'> = {
  Approved: 'success',
  Rejected: 'error',
  Pending: 'warning',
};

export default function RequestHistory({ requests, loading, showEmployee }: Props) {
  const [typeFilter, setTypeFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');

  const filtered = requests.filter((r) => {
    if (typeFilter && r.request_type !== typeFilter) return false;
    if (statusFilter && r.Status !== statusFilter) return false;
    return true;
  });

  const columns: GridColDef[] = [
    { field: 'request_type', headerName: 'Type', width: 130 },
    ...(showEmployee
      ? [{ field: 'Title', headerName: 'Employee', width: 180, valueGetter: (_: any, row: any) => (row.Title || '').split(' /// ')[0] }]
      : []),
    { field: 'LeaveType', headerName: 'Leave Type', width: 160 },
    { field: 'StartDate', headerName: 'Start', width: 120 },
    { field: 'EndDate', headerName: 'End', width: 120 },
    { field: 'Days', headerName: 'Days', width: 80, type: 'number' },
    { field: 'Hours', headerName: 'Hours', width: 80, type: 'number' },
    {
      field: 'Status',
      headerName: 'Status',
      width: 120,
      renderCell: (params) => (
        <Chip label={params.value || 'Unknown'} color={statusColor[params.value as string] || 'default'} size="small" />
      ),
    },
  ];

  return (
    <Box>
      <Box sx={{ display: 'flex', gap: 2, mb: 2 }}>
        <FormControl size="small" sx={{ minWidth: 140 }}>
          <InputLabel>Type</InputLabel>
          <Select value={typeFilter} label="Type" onChange={(e) => setTypeFilter(e.target.value)}>
            <MenuItem value="">All</MenuItem>
            <MenuItem value="leave">Leave</MenuItem>
            <MenuItem value="overtime">Overtime</MenuItem>
            <MenuItem value="carryover-payout">Carry Over / Payout</MenuItem>
          </Select>
        </FormControl>
        <FormControl size="small" sx={{ minWidth: 140 }}>
          <InputLabel>Status</InputLabel>
          <Select value={statusFilter} label="Status" onChange={(e) => setStatusFilter(e.target.value)}>
            <MenuItem value="">All</MenuItem>
            <MenuItem value="Pending">Pending</MenuItem>
            <MenuItem value="Approved">Approved</MenuItem>
            <MenuItem value="Rejected">Rejected</MenuItem>
          </Select>
        </FormControl>
      </Box>
      <DataGrid
        rows={filtered}
        columns={columns}
        loading={loading}
        getRowId={(row) => `${row.request_type}-${row.id}`}
        initialState={{ pagination: { paginationModel: { pageSize: 10 } } }}
        pageSizeOptions={[10, 25, 50]}
        autoHeight
        disableRowSelectionOnClick
      />
    </Box>
  );
}
