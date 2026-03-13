import { useState } from 'react';
import { DataGrid, GridColDef } from '@mui/x-data-grid';
import { Box, Chip, FormControl, InputLabel, Select, MenuItem, Button } from '@mui/material';

interface Props {
  requests: any[];
  loading?: boolean;
  showEmployee?: boolean;
  onRefund?: (type: string, id: string) => void;
  processingEnabled?: boolean;
  actionLoading?: string | null;
}

const statusColor: Record<string, 'success' | 'error' | 'warning' | 'info' | 'default'> = {
  Approved: 'success',
  Rejected: 'error',
  Pending: 'warning',
  Refunded: 'info',
};

export default function RequestHistory({ requests, loading, showEmployee, onRefund, processingEnabled, actionLoading }: Props) {
  const [typeFilter, setTypeFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');

  const filtered = requests.filter((r) => {
    if (typeFilter && r.request_type !== typeFilter) return false;
    if (statusFilter && r.Status !== statusFilter) return false;
    return true;
  });

  const columns: GridColDef[] = [
    {
      field: 'request_type',
      headerName: 'Type',
      width: 150,
      valueGetter: (_value: string, row: any) => {
        if (row.request_type === 'leave') return 'Leave';
        if (row.request_type === 'overtime') return 'Overtime';
        if (row.request_type === 'carryover-payout') return row.TypeofRequest || 'Carry Over / Payout';
        return row.request_type;
      },
    },
    ...(showEmployee
      ? [{ field: 'employee_name', headerName: 'Employee', width: 180 }]
      : []),
    { field: 'LeaveType', headerName: 'Leave Type', width: 160 },
    {
      field: 'StartDate',
      headerName: 'Start',
      width: 120,
      valueGetter: (_value: string, row: any) =>
        row.StartDate || (row.Created ? row.Created.split('T')[0] : ''),
    },
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
    ...(onRefund
      ? [{
          field: '_actions',
          headerName: 'Actions',
          width: 120,
          sortable: false,
          filterable: false,
          renderCell: (params: any) => {
            if (params.row.Status !== 'Approved') return null;
            const key = `${params.row.request_type}-${params.row.id}`;
            return (
              <Button
                size="small"
                variant="outlined"
                color="warning"
                disabled={!processingEnabled || actionLoading === key}
                onClick={() => onRefund(params.row.request_type, String(params.row.id))}
              >
                {actionLoading === key ? 'Refunding...' : 'Refund'}
              </Button>
            );
          },
        } as GridColDef]
      : []),
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
            <MenuItem value="Refunded">Refunded</MenuItem>
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
