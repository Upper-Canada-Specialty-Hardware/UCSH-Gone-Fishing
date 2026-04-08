import { useState, useMemo } from 'react';
import {
  Box, Alert, Chip, Stack,
  Dialog, DialogTitle, DialogContent, DialogActions,
  Button, TextField, Typography, IconButton, CircularProgress,
} from '@mui/material';
import { DataGrid, GridColDef, GridActionsCellItem, GridActionsCellItemProps } from '@mui/x-data-grid';
import ReplayIcon from '@mui/icons-material/Replay';
import CloseIcon from '@mui/icons-material/Close';
import { SHARED_DATA_GRID_PROPS, getDescription, getStartDate } from './dataGridDefaults';

const DIAGNOSTIC_LABELS: Record<string, { label: string; color: 'error' | 'warning' }> = {
  missing_dates:          { label: 'Missing Dates',           color: 'error' },
  missing_employee:       { label: 'Employee Not Found',      color: 'error' },
  missing_all_managers:   { label: 'No Managers (Staff Dir)',  color: 'error' },
  missing_days:           { label: 'Days Not Calculated',     color: 'warning' },
  missing_manager_lookup: { label: 'Manager Lookup Failed',   color: 'warning' },
  approval_email_pending: { label: 'Approval Email Pending',  color: 'warning' },
};

interface Props {
  stuckRequests: any[];
  processingEnabled: boolean;
  onReprocess: (id: string, reason: string) => Promise<void>;
  actionLoading: string | null;
}

export default function StuckRequests({ stuckRequests, processingEnabled, onReprocess, actionLoading }: Props) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogItem, setDialogItem] = useState<any>(null);
  const [reason, setReason] = useState('');

  const openDialog = (item: any) => {
    setDialogItem(item);
    setReason('');
    setDialogOpen(true);
  };

  const closeDialog = () => {
    setDialogOpen(false);
    setDialogItem(null);
    setReason('');
  };

  const handleSubmit = async () => {
    if (!dialogItem || !reason.trim()) return;
    await onReprocess(String(dialogItem.id), reason.trim());
    closeDialog();
  };

  const rows = useMemo(
    () =>
      stuckRequests.map((r) => ({
        id: r.id,
        _raw: r,
        employee_name: r.employee_name || '',
        LeaveType: r.LeaveType || '',
        description: getDescription({ ...r, request_type: 'leave' }),
        StartDate: getStartDate(r),
        EndDate: r.EndDate || '',
        Days: r.Days ?? null,
        Created: r.Created ? r.Created.split('T')[0] : '',
        diagnostics: r.diagnostics || [],
        diagnostic_detail: r.diagnostic_detail || '',
      })),
    [stuckRequests],
  );

  const columns = useMemo<GridColDef[]>(() => {
    const cols: GridColDef[] = [
      { field: 'id', headerName: 'ID', width: 70 },
      { field: 'employee_name', headerName: 'Employee', width: 180 },
      { field: 'LeaveType', headerName: 'Leave Type', width: 140 },
      { field: 'description', headerName: 'Description', width: 200 },
      { field: 'StartDate', headerName: 'Start', width: 120 },
      { field: 'EndDate', headerName: 'End', width: 120 },
      { field: 'Days', headerName: 'Days', width: 80, type: 'number' },
      { field: 'Created', headerName: 'Created', width: 120 },
      {
        field: 'diagnostics',
        headerName: 'Issues',
        width: 250,
        sortable: false,
        filterable: false,
        renderCell: (params) => (
          <Stack direction="row" spacing={0.5} flexWrap="wrap" alignItems="center" sx={{ py: 0.5 }}>
            {(params.value as string[]).map((code: string) => {
              const def = DIAGNOSTIC_LABELS[code] || { label: code, color: 'warning' as const };
              return <Chip key={code} label={def.label} color={def.color} size="small" />;
            })}
          </Stack>
        ),
      },
      {
        field: 'actions',
        headerName: 'Actions',
        type: 'actions',
        width: 100,
        getActions: (params) => {
          const raw = params.row._raw;
          const loading = actionLoading === `reprocess-${raw.id}`;
          const actions: React.ReactElement<GridActionsCellItemProps>[] = [
            <GridActionsCellItem
              key="reprocess"
              icon={loading ? <CircularProgress size={20} /> : <ReplayIcon color="primary" />}
              label="Reprocess"
              disabled={!processingEnabled || loading}
              onClick={() => openDialog(raw)}
            />,
          ];
          return actions;
        },
      },
    ];
    return cols;
  }, [processingEnabled, actionLoading]);

  if (stuckRequests.length === 0) {
    return <Alert severity="success" sx={{ mt: 1 }}>No stuck requests found.</Alert>;
  }

  return (
    <Box>
      <DataGrid
        rows={rows}
        columns={columns}
        {...SHARED_DATA_GRID_PROPS}
        initialState={{
          ...SHARED_DATA_GRID_PROPS.initialState,
          sorting: { sortModel: [{ field: 'Created', sort: 'desc' }] },
        }}
      />

      <Dialog open={dialogOpen} onClose={closeDialog} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          Reprocess Leave Request #{dialogItem?.id}
          <IconButton onClick={closeDialog} size="small">
            <CloseIcon />
          </IconButton>
        </DialogTitle>
        <DialogContent dividers>
          {dialogItem && (
            <Box sx={{ mb: 2 }}>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                <strong>Employee:</strong> {dialogItem.employee_name || 'Unknown'}
              </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                <strong>Diagnostic:</strong> {dialogItem.diagnostic_detail}
              </Typography>
              <Stack direction="row" spacing={0.5} sx={{ mb: 2 }}>
                {(dialogItem.diagnostics || []).map((code: string) => {
                  const def = DIAGNOSTIC_LABELS[code] || { label: code, color: 'warning' as const };
                  return <Chip key={code} label={def.label} color={def.color} size="small" />;
                })}
              </Stack>
            </Box>
          )}
          <TextField
            label="Reason for reprocessing"
            placeholder="e.g. Fixed AllManagers field in Staff Directory"
            multiline
            rows={3}
            fullWidth
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            required
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={closeDialog}>Cancel</Button>
          <Button
            variant="contained"
            onClick={handleSubmit}
            disabled={!reason.trim() || actionLoading === `reprocess-${dialogItem?.id}`}
            startIcon={
              actionLoading === `reprocess-${dialogItem?.id}`
                ? <CircularProgress size={16} color="inherit" />
                : <ReplayIcon />
            }
          >
            Reprocess
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
