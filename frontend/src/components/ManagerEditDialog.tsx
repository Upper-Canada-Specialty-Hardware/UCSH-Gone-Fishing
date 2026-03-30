import { useState, useEffect } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions,
  Button, Chip, Autocomplete, TextField, Box, Typography,
  CircularProgress,
} from '@mui/material';
import { updateManagerAssignment } from '../api/client';

interface SpUser {
  sp_user_id: number;
  name: string;
  email: string;
  department: string;
}

interface Manager {
  sp_user_id: number;
  name: string;
}

interface Employee {
  id: string;
  name: string;
  department: string;
  location: string;
  managers: Manager[];
}

interface Props {
  open: boolean;
  employee: Employee | null;
  spUsers: SpUser[];
  onClose: () => void;
  onSaved: () => void;
}

export default function ManagerEditDialog({ open, employee, spUsers, onClose, onSaved }: Props) {
  const [selectedManagers, setSelectedManagers] = useState<SpUser[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (employee) {
      const current = employee.managers.map((m) => {
        const match = spUsers.find((u) => u.sp_user_id === m.sp_user_id);
        return match || { sp_user_id: m.sp_user_id, name: m.name, email: '', department: '' };
      });
      setSelectedManagers(current);
      setError('');
    }
  }, [employee, spUsers]);

  const handleSave = async () => {
    if (!employee) return;
    setSaving(true);
    setError('');
    try {
      await updateManagerAssignment(employee.id, selectedManagers.map((m) => m.sp_user_id));
      onSaved();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to update managers');
    } finally {
      setSaving(false);
    }
  };

  if (!employee) return null;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Edit Managers for {employee.name}</DialogTitle>
      <DialogContent>
        <Box sx={{ mb: 2, mt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            {employee.department} &mdash; {employee.location}
          </Typography>
        </Box>

        <Autocomplete
          multiple
          options={spUsers}
          value={selectedManagers}
          onChange={(_, val) => setSelectedManagers(val)}
          getOptionLabel={(opt) => opt.name}
          isOptionEqualToValue={(opt, val) => opt.sp_user_id === val.sp_user_id}
          renderOption={(props, opt) => (
            <li {...props} key={opt.sp_user_id}>
              <Box>
                <Typography variant="body2">{opt.name}</Typography>
                <Typography variant="caption" color="text.secondary">
                  {opt.department} &mdash; {opt.email}
                </Typography>
              </Box>
            </li>
          )}
          renderTags={(value, getTagProps) =>
            value.map((opt, index) => (
              <Chip
                label={opt.name}
                {...getTagProps({ index })}
                key={opt.sp_user_id}
              />
            ))
          }
          renderInput={(params) => (
            <TextField {...params} label="Managers" placeholder="Add manager..." />
          )}
        />

        {error && (
          <Typography color="error" variant="body2" sx={{ mt: 1 }}>
            {error}
          </Typography>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={saving}>Cancel</Button>
        <Button onClick={handleSave} variant="contained" disabled={saving}>
          {saving ? <CircularProgress size={20} /> : 'Save'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
