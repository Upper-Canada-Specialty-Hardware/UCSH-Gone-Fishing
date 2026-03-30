import { useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions,
  Button, Stepper, Step, StepLabel, Box, Typography,
  Autocomplete, TextField, Card, CardActionArea, CardContent,
  CircularProgress, Alert, Chip,
} from '@mui/material';
import { DataGrid, GridColDef } from '@mui/x-data-grid';
import SwapHorizIcon from '@mui/icons-material/SwapHoriz';
import PersonAddIcon from '@mui/icons-material/PersonAdd';
import PersonRemoveIcon from '@mui/icons-material/PersonRemove';
import { bulkManagerAssignment } from '../api/client';

interface SpUser {
  sp_user_id: number;
  name: string;
  email: string;
  department: string;
}

interface Props {
  open: boolean;
  spUsers: SpUser[];
  onClose: () => void;
  onComplete: () => void;
}

type Operation = 'replace' | 'add' | 'remove';

const STEPS = ['Choose Operation', 'Parameters', 'Preview', 'Apply'];

const OPERATIONS: { key: Operation; label: string; description: string; icon: React.ReactNode }[] = [
  {
    key: 'replace',
    label: 'Replace Manager',
    description: 'Swap one manager for another across all their reports.',
    icon: <SwapHorizIcon sx={{ fontSize: 40 }} />,
  },
  {
    key: 'add',
    label: 'Add Manager',
    description: 'Add a manager to employees of another manager.',
    icon: <PersonAddIcon sx={{ fontSize: 40 }} />,
  },
  {
    key: 'remove',
    label: 'Remove Manager',
    description: 'Remove a manager from all their reports.',
    icon: <PersonRemoveIcon sx={{ fontSize: 40 }} />,
  },
];

export default function ManagerWizardDialog({ open, spUsers, onClose, onComplete }: Props) {
  const [activeStep, setActiveStep] = useState(0);
  const [operation, setOperation] = useState<Operation | null>(null);
  const [sourceManager, setSourceManager] = useState<SpUser | null>(null);
  const [targetManager, setTargetManager] = useState<SpUser | null>(null);
  const [preview, setPreview] = useState<any>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [applyResult, setApplyResult] = useState<any>(null);
  const [applyLoading, setApplyLoading] = useState(false);
  const [error, setError] = useState('');

  const reset = () => {
    setActiveStep(0);
    setOperation(null);
    setSourceManager(null);
    setTargetManager(null);
    setPreview(null);
    setApplyResult(null);
    setError('');
    setPreviewLoading(false);
    setApplyLoading(false);
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  const handleSelectOperation = (op: Operation) => {
    setOperation(op);
    setSourceManager(null);
    setTargetManager(null);
    setActiveStep(1);
  };

  const canProceedToPreview = () => {
    if (!operation) return false;
    if (operation === 'replace') return !!sourceManager && !!targetManager;
    if (operation === 'add') return !!sourceManager && !!targetManager;
    if (operation === 'remove') return !!sourceManager;
    return false;
  };

  const handlePreview = async () => {
    if (!operation) return;
    setPreviewLoading(true);
    setError('');
    try {
      const params: any = { operation, preview: true };
      if (sourceManager) params.source_manager_id = sourceManager.sp_user_id;
      if (targetManager) params.target_manager_id = targetManager.sp_user_id;
      const res = await bulkManagerAssignment(params);
      setPreview(res.data);
      setActiveStep(2);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Preview failed');
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleApply = async () => {
    if (!operation) return;
    setApplyLoading(true);
    setError('');
    try {
      const params: any = { operation, preview: false };
      if (sourceManager) params.source_manager_id = sourceManager.sp_user_id;
      if (targetManager) params.target_manager_id = targetManager.sp_user_id;
      const res = await bulkManagerAssignment(params);
      setApplyResult(res.data);
      setActiveStep(3);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Operation failed');
    } finally {
      setApplyLoading(false);
    }
  };

  const handleDone = () => {
    reset();
    onComplete();
  };

  const previewColumns: GridColDef[] = [
    { field: 'name', headerName: 'Employee', flex: 1 },
    { field: 'department', headerName: 'Department', width: 140 },
    {
      field: 'current_managers',
      headerName: 'Current Managers',
      flex: 1,
      renderCell: (params) => (
        <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap', py: 0.5 }}>
          {(params.value as string[])?.map((n: string, i: number) => (
            <Chip key={i} label={n} size="small" />
          ))}
        </Box>
      ),
    },
    {
      field: 'new_managers',
      headerName: 'New Managers',
      flex: 1,
      renderCell: (params) => (
        <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap', py: 0.5 }}>
          {(params.value as string[])?.map((n: string, i: number) => (
            <Chip key={i} label={n} size="small" color="primary" />
          ))}
        </Box>
      ),
    },
  ];

  const resultColumns: GridColDef[] = [
    { field: 'name', headerName: 'Employee', flex: 1 },
    {
      field: 'status',
      headerName: 'Status',
      width: 120,
      renderCell: (params) => (
        <Chip
          label={params.value}
          size="small"
          color={params.value === 'success' ? 'success' : 'error'}
        />
      ),
    },
    { field: 'detail', headerName: 'Detail', flex: 1 },
  ];

  return (
    <Dialog open={open} onClose={handleClose} maxWidth="md" fullWidth>
      <DialogTitle>Manage Assignments</DialogTitle>
      <DialogContent>
        <Stepper activeStep={activeStep} sx={{ mb: 3, mt: 1 }}>
          {STEPS.map((label) => (
            <Step key={label}><StepLabel>{label}</StepLabel></Step>
          ))}
        </Stepper>

        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

        {/* Step 0: Choose Operation */}
        {activeStep === 0 && (
          <Box sx={{ display: 'flex', gap: 2 }}>
            {OPERATIONS.map((op) => (
              <Card
                key={op.key}
                variant="outlined"
                sx={{
                  flex: 1,
                  border: operation === op.key ? '2px solid' : undefined,
                  borderColor: operation === op.key ? 'primary.main' : undefined,
                }}
              >
                <CardActionArea onClick={() => handleSelectOperation(op.key)}>
                  <CardContent sx={{ textAlign: 'center', py: 3 }}>
                    {op.icon}
                    <Typography variant="h6" sx={{ mt: 1 }}>{op.label}</Typography>
                    <Typography variant="body2" color="text.secondary">{op.description}</Typography>
                  </CardContent>
                </CardActionArea>
              </Card>
            ))}
          </Box>
        )}

        {/* Step 1: Parameters */}
        {activeStep === 1 && operation && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {(operation === 'replace' || operation === 'add' || operation === 'remove') && (
              <Autocomplete
                options={spUsers}
                value={sourceManager}
                onChange={(_, val) => setSourceManager(val)}
                getOptionLabel={(opt) => `${opt.name} (${opt.department})`}
                isOptionEqualToValue={(opt, val) => opt.sp_user_id === val.sp_user_id}
                renderInput={(params) => (
                  <TextField
                    {...params}
                    label={operation === 'remove' ? 'Manager to Remove' : 'Current Manager'}
                  />
                )}
              />
            )}
            {(operation === 'replace' || operation === 'add') && (
              <Autocomplete
                options={spUsers}
                value={targetManager}
                onChange={(_, val) => setTargetManager(val)}
                getOptionLabel={(opt) => `${opt.name} (${opt.department})`}
                isOptionEqualToValue={(opt, val) => opt.sp_user_id === val.sp_user_id}
                renderInput={(params) => (
                  <TextField
                    {...params}
                    label={operation === 'replace' ? 'Replacement Manager' : 'Manager to Add'}
                  />
                )}
              />
            )}
          </Box>
        )}

        {/* Step 2: Preview */}
        {activeStep === 2 && preview && (
          <Box>
            <Typography variant="body1" sx={{ mb: 2 }}>
              {preview.affected_count} employee(s) will be affected:
            </Typography>
            <DataGrid
              rows={preview.affected_employees || []}
              columns={previewColumns}
              getRowId={(row) => row.id}
              pageSizeOptions={[10, 25]}
              initialState={{ pagination: { paginationModel: { pageSize: 10 } } }}
              autoHeight
              disableRowSelectionOnClick
              getRowHeight={() => 'auto'}
            />
          </Box>
        )}

        {/* Step 3: Results */}
        {activeStep === 3 && applyResult && (
          <Box>
            <Alert severity="success" sx={{ mb: 2 }}>
              {applyResult.success} of {applyResult.total} updated successfully.
            </Alert>
            <DataGrid
              rows={applyResult.results || []}
              columns={resultColumns}
              getRowId={(row) => row.id}
              pageSizeOptions={[10, 25]}
              initialState={{ pagination: { paginationModel: { pageSize: 10 } } }}
              autoHeight
              disableRowSelectionOnClick
            />
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        {activeStep < 3 && (
          <Button onClick={handleClose}>Cancel</Button>
        )}
        {activeStep === 1 && (
          <>
            <Button onClick={() => setActiveStep(0)}>Back</Button>
            <Button
              variant="contained"
              onClick={handlePreview}
              disabled={!canProceedToPreview() || previewLoading}
            >
              {previewLoading ? <CircularProgress size={20} /> : 'Preview Changes'}
            </Button>
          </>
        )}
        {activeStep === 2 && (
          <>
            <Button onClick={() => setActiveStep(1)}>Back</Button>
            <Button
              variant="contained"
              color="warning"
              onClick={handleApply}
              disabled={applyLoading || !preview?.affected_count}
            >
              {applyLoading ? <CircularProgress size={20} /> : `Apply to ${preview?.affected_count} Employees`}
            </Button>
          </>
        )}
        {activeStep === 3 && (
          <Button variant="contained" onClick={handleDone}>Done</Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
