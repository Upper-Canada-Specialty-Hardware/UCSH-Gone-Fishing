import { useEffect, useState, useCallback } from 'react';
import {
  Box, Button, Chip, Typography, CircularProgress, Alert, Snackbar, Paper,
} from '@mui/material';
import { DataGrid, GridColDef } from '@mui/x-data-grid';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import { getManagerAssignments, getSpUsers } from '../api/client';
import { SHARED_DATA_GRID_PROPS } from './dataGridDefaults';
import ManagerEditDialog from './ManagerEditDialog';
import ManagerWizardDialog from './ManagerWizardDialog';

export default function ManagerAssignments() {
  const [assignments, setAssignments] = useState<any[]>([]);
  const [spUsers, setSpUsers] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [editEmployee, setEditEmployee] = useState<any | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [snack, setSnack] = useState({ open: false, message: '', severity: 'success' as 'success' | 'error' });

  const loadData = useCallback(async () => {
    try {
      const [assignRes, usersRes] = await Promise.all([
        getManagerAssignments(),
        getSpUsers(),
      ]);
      setAssignments(assignRes.data.assignments || []);
      setSpUsers(usersRes.data.users || []);
    } catch {
      setSnack({ open: true, message: 'Failed to load data', severity: 'error' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const handleEditSaved = () => {
    setEditEmployee(null);
    setSnack({ open: true, message: 'Managers updated', severity: 'success' });
    loadData();
  };

  const handleWizardComplete = () => {
    setWizardOpen(false);
    setSnack({ open: true, message: 'Bulk operation complete', severity: 'success' });
    loadData();
  };

  const columns: GridColDef[] = [
    { field: 'name', headerName: 'Employee', width: 200 },
    { field: 'department', headerName: 'Department', width: 140 },
    { field: 'location', headerName: 'Location', width: 160 },
    {
      field: 'managers',
      headerName: 'Current Managers',
      flex: 1,
      minWidth: 250,
      sortable: false,
      renderCell: (params) => {
        const managers = params.value as any[];
        if (!managers || managers.length === 0) {
          return (
            <Chip
              icon={<WarningAmberIcon />}
              label="No manager"
              size="small"
              color="warning"
              variant="outlined"
            />
          );
        }
        return (
          <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap', py: 0.5 }}>
            {managers.map((m: any) => (
              <Chip key={m.sp_user_id} label={m.name} size="small" />
            ))}
          </Box>
        );
      },
    },
  ];

  if (loading) {
    return <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress /></Box>;
  }

  const noManagerCount = assignments.filter((a) => !a.managers || a.managers.length === 0).length;

  return (
    <Paper sx={{ p: 3 }}>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
        <Box>
          <Typography variant="h6">Manager Assignments</Typography>
          {noManagerCount > 0 && (
            <Alert severity="warning" sx={{ mt: 1, py: 0 }}>
              {noManagerCount} employee(s) have no manager assigned.
            </Alert>
          )}
        </Box>
        <Button variant="contained" onClick={() => setWizardOpen(true)}>
          Manage Assignments
        </Button>
      </Box>

      <DataGrid
        rows={assignments}
        columns={columns}
        onRowClick={(params) => setEditEmployee(params.row)}
        getRowHeight={() => 'auto'}
        sx={{ cursor: 'pointer' }}
        {...SHARED_DATA_GRID_PROPS}
      />

      <ManagerEditDialog
        open={!!editEmployee}
        employee={editEmployee}
        spUsers={spUsers}
        onClose={() => setEditEmployee(null)}
        onSaved={handleEditSaved}
      />

      <ManagerWizardDialog
        open={wizardOpen}
        spUsers={spUsers}
        onClose={() => setWizardOpen(false)}
        onComplete={handleWizardComplete}
      />

      <Snackbar
        open={snack.open}
        autoHideDuration={4000}
        onClose={() => setSnack((s) => ({ ...s, open: false }))}
        message={snack.message}
      />
    </Paper>
  );
}
