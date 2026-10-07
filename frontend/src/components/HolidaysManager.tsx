import { useEffect, useState, useCallback } from 'react';
import {
  Box, Button, Typography, CircularProgress, Snackbar, Alert, Paper,
  Dialog, DialogTitle, DialogContent, DialogActions, TextField, MenuItem,
} from '@mui/material';
import { DataGrid, GridColDef } from '@mui/x-data-grid';
import AddIcon from '@mui/icons-material/Add';
import {
  getAdminHolidays, createHoliday, updateHoliday, deleteHoliday,
  getHolidaysParity, copyHolidaysToPostgres,
} from '../api/client';
import { SHARED_DATA_GRID_PROPS } from './dataGridDefaults';

/** One holiday row as the grid shows it. */
interface HolidayRow {
  id: string;
  title: string;
  date: string;
  province: string;
}

/** The SharePoint vs Postgres parity report from the backend (snake_case wire shape). */
interface ParityReport {
  in_parity: boolean;
  sharepoint_count: number;
  postgres_count: number;
  only_in_sharepoint: string[];
  only_in_postgres: string[];
  mismatched: { id: string; differences: Record<string, { sharepoint: string; postgres: string }> }[];
}

/** Provinces the location map produces, plus blank for company-wide rows. */
const PROVINCES = ['', 'ON', 'BC', 'NL'];

/** An empty editor form (add mode). */
const EMPTY_FORM = { title: '', date: '', province: '' };

/**
 * Admin holidays editor and the SharePoint -> Postgres copy / parity panel.
 *
 * Lists every holiday (and the Half Fridays START/END season markers), with
 * add / edit / delete going through the backend's /admin/holidays endpoints,
 * which write through the repository seam so this works identically whichever
 * storage backend is selected. The parity panel compares both backends and
 * offers a one-time copy from SharePoint into Postgres.
 *
 * @returns The holidays management panel.
 */
export default function HolidaysManager() {
  const [rows, setRows] = useState<HolidayRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);             // disables dialog buttons in flight
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null); // null = add mode
  const [form, setForm] = useState(EMPTY_FORM);
  const [deleteTarget, setDeleteTarget] = useState<HolidayRow | null>(null); // confirm dialog
  const [parity, setParity] = useState<ParityReport | null>(null); // null until first fetched
  const [copying, setCopying] = useState(false);           // disables the copy button in flight
  const [snack, setSnack] = useState({ open: false, message: '', severity: 'success' as 'success' | 'error' });

  /**
   * Reload the grid and the parity report from the backend.
   *
   * @returns A promise that resolves once both have been fetched.
   */
  const loadData = useCallback(async () => {
    try {
      const res = await getAdminHolidays();
      // Flatten {"id","fields"} items into flat grid rows.
      setRows((res.data.holidays || []).map((item: any) => ({
        id: String(item.id),
        title: item.fields?.Title ?? '',
        date: item.fields?.Date ? String(item.fields.Date).slice(0, 10) : '',
        province: item.fields?.Province ?? '',
      })));
    } catch {
      setSnack({ open: true, message: 'Failed to load holidays', severity: 'error' });
    } finally {
      setLoading(false);
    }
    // Parity is best-effort: a failure here must not blank the grid.
    try {
      const res = await getHolidaysParity();
      setParity(res.data);
    } catch {
      setParity(null);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  /**
   * Open the editor dialog, prefilled for edit or blank for add.
   *
   * @param row - The row to edit, or undefined to add a new holiday.
   */
  const openEditor = (row?: HolidayRow) => {
    setEditingId(row ? row.id : null);
    setForm(row ? { title: row.title, date: row.date, province: row.province } : EMPTY_FORM);
    setEditorOpen(true);
  };

  /**
   * Create or update through the API, then refresh the grid.
   *
   * @returns A promise that resolves once the save attempt finishes.
   */
  const handleSave = async () => {
    setSaving(true);
    try {
      if (editingId === null) {
        await createHoliday(form);                         // add mode
      } else {
        await updateHoliday(editingId, form);              // edit mode
      }
      setEditorOpen(false);
      setSnack({ open: true, message: editingId === null ? 'Holiday added' : 'Holiday updated', severity: 'success' });
      loadData();
    } catch (err: any) {
      // Surface the backend's validation message (400) rather than a generic error.
      const detail = err?.response?.data?.detail || 'Save failed';
      setSnack({ open: true, message: detail, severity: 'error' });
    } finally {
      setSaving(false);
    }
  };

  /**
   * Delete the confirmed row, then refresh the grid.
   *
   * @returns A promise that resolves once the delete attempt finishes.
   */
  const handleDelete = async () => {
    if (!deleteTarget) return;
    setSaving(true);
    try {
      await deleteHoliday(deleteTarget.id);
      setSnack({ open: true, message: 'Holiday deleted', severity: 'success' });
      setDeleteTarget(null);
      loadData();
    } catch (err: any) {
      const detail = err?.response?.data?.detail || 'Delete failed';
      setSnack({ open: true, message: detail, severity: 'error' });
    } finally {
      setSaving(false);
    }
  };

  /**
   * Run the one-time SharePoint -> Postgres copy, then refresh the parity panel.
   *
   * @returns A promise that resolves once the copy attempt finishes.
   */
  const handleCopy = async () => {
    setCopying(true);
    try {
      const res = await copyHolidaysToPostgres();
      const { source_count, created, updated } = res.data;
      setSnack({
        open: true,
        message: `Copied ${source_count} holidays (${created} new, ${updated} updated)`,
        severity: 'success',
      });
      loadData();
    } catch (err: any) {
      const detail = err?.response?.data?.detail || 'Copy failed';
      setSnack({ open: true, message: detail, severity: 'error' });
    } finally {
      setCopying(false);
    }
  };

  const columns: GridColDef[] = [
    { field: 'title', headerName: 'Holiday', flex: 1, minWidth: 220 },
    { field: 'date', headerName: 'Date', width: 130 },
    { field: 'province', headerName: 'Province', width: 110 },
    {
      field: 'actions',
      headerName: '',
      width: 160,
      sortable: false,
      renderCell: (params) => (
        <Box>
          <Button size="small" onClick={() => openEditor(params.row)}>Edit</Button>
          <Button size="small" color="error" onClick={() => setDeleteTarget(params.row)}>Delete</Button>
        </Box>
      ),
    },
  ];

  if (loading) return <Box sx={{ textAlign: 'center', py: 6 }}><CircularProgress /></Box>;

  return (
    <Paper sx={{ p: 2 }}>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
        <Box>
          <Typography variant="h6">Company Holidays</Typography>
          <Typography variant="body2" color="text.secondary">
            Stat holidays skip leave-day counting. "Half Fridays START" / "Half
            Fridays END" rows mark the half-day-Friday season.
          </Typography>
        </Box>
        <Button variant="contained" startIcon={<AddIcon />} onClick={() => openEditor()}>
          Add Holiday
        </Button>
      </Box>

      <DataGrid rows={rows} columns={columns} {...SHARED_DATA_GRID_PROPS} />

      {/* SharePoint -> Postgres copy + parity panel */}
      <Box sx={{ mt: 3, p: 2, border: 1, borderColor: 'divider', borderRadius: 1 }}>
        <Typography variant="subtitle1" sx={{ mb: 1 }}>Postgres storage</Typography>
        {parity === null ? (
          <Typography variant="body2" color="text.secondary">Parity check unavailable.</Typography>
        ) : (
          <>
            <Typography variant="body2" sx={{ mb: 1 }}>
              SharePoint: {parity.sharepoint_count} holidays. Postgres: {parity.postgres_count} holidays.
            </Typography>
            {parity.in_parity ? (
              <Alert severity="success" sx={{ mb: 1 }}>
                Postgres matches SharePoint. STORAGE_HOLIDAYS can be flipped to
                "postgres" safely.
              </Alert>
            ) : (
              <Alert severity="warning" sx={{ mb: 1 }}>
                Postgres does not match SharePoint yet. Copy from SharePoint, then
                re-check before flipping STORAGE_HOLIDAYS.
                {parity.only_in_sharepoint.length > 0 && (
                  <> Missing from Postgres: {parity.only_in_sharepoint.join(', ')}.</>
                )}
                {parity.only_in_postgres.length > 0 && (
                  <> Only in Postgres: {parity.only_in_postgres.join(', ')}.</>
                )}
                {parity.mismatched.length > 0 && (
                  <> Differing rows: {parity.mismatched.map((m) => m.id).join(', ')}.</>
                )}
              </Alert>
            )}
          </>
        )}
        <Button variant="outlined" onClick={handleCopy} disabled={copying}>
          {copying ? 'Copying…' : 'Copy from SharePoint'}
        </Button>
      </Box>

      {/* Add / edit dialog */}
      <Dialog open={editorOpen} onClose={() => setEditorOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>{editingId === null ? 'Add Holiday' : 'Edit Holiday'}</DialogTitle>
        <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: '8px !important' }}>
          <TextField
            label="Name"
            value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
            required
            fullWidth
          />
          <TextField
            label="Date"
            type="date"
            value={form.date}
            onChange={(e) => setForm({ ...form, date: e.target.value })}
            required
            fullWidth
            InputLabelProps={{ shrink: true }}
          />
          <TextField
            label="Province"
            select
            value={form.province}
            onChange={(e) => setForm({ ...form, province: e.target.value })}
            fullWidth
            helperText="Blank applies to no province filter (e.g. season markers)"
          >
            {PROVINCES.map((p) => (
              <MenuItem key={p || 'none'} value={p}>{p || '(none)'}</MenuItem>
            ))}
          </TextField>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEditorOpen(false)} disabled={saving}>Cancel</Button>
          <Button variant="contained" onClick={handleSave} disabled={saving || !form.title.trim() || !form.date}>
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Delete confirmation */}
      <Dialog open={deleteTarget !== null} onClose={() => setDeleteTarget(null)} maxWidth="xs" fullWidth>
        <DialogTitle>Delete holiday?</DialogTitle>
        <DialogContent>
          <Typography>
            Remove <strong>{deleteTarget?.title}</strong>
            {deleteTarget?.date ? ` (${deleteTarget.date})` : ''}? Leave-day
            calculations will stop treating this date as a holiday.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleteTarget(null)} disabled={saving}>Cancel</Button>
          <Button color="error" variant="contained" onClick={handleDelete} disabled={saving}>
            {saving ? 'Deleting…' : 'Delete'}
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={snack.open}
        autoHideDuration={4000}
        onClose={() => setSnack({ ...snack, open: false })}
      >
        <Alert severity={snack.severity} onClose={() => setSnack({ ...snack, open: false })}>
          {snack.message}
        </Alert>
      </Snackbar>
    </Paper>
  );
}
