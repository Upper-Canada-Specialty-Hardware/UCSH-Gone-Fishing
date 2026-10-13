import { useEffect, useState } from 'react';
import { Alert, Box, Button, Chip, Paper, TextField, Typography } from '@mui/material';
import FileDownloadOutlinedIcon from '@mui/icons-material/FileDownloadOutlined';
import {
  CarryoverWindow, exportCarryoverPayout, getCarryoverPayoutWindow, setCarryoverPayoutWindow,
} from '../../api/client';
import { longDate } from '../../utils/carryoverWindow';

/**
 * The dates carry-over and payout requests are accepted between, and the
 * Excel export of everyone's carry-over and payout information.
 *
 * HR takes these requests at one time of year. With no dates set they are
 * closed; outside the dates the request page greys them out and the backend
 * refuses new ones. Requests already sent keep going through approval, and
 * the Microsoft Form stays under HR's control on the bulletin board.
 *
 * @returns A card for the admin home.
 */
export default function CarryoverWindowCard() {
  const [saved, setSaved] = useState<CarryoverWindow | null>(null);   // what the server holds; null until loaded
  const [start, setStart] = useState('');                              // the form's dates, YYYY-MM-DD
  const [end, setEnd] = useState('');
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState('');

  /** Show what the server stored, and put it in the form. @param w - The stored window. */
  const show = (w: CarryoverWindow) => { setSaved(w); setStart(w.start || ''); setEnd(w.end || ''); };

  useEffect(() => {
    getCarryoverPayoutWindow()
      .then((r) => show(r.data))
      .catch(() => setError('Could not read the carry-over and payout dates.'));
  }, []);

  /**
   * Store new dates, or none to close.
   *
   * @param s - First day, or null to clear.
   * @param e - Last day, or null to clear.
   */
  const save = async (s: string | null, e: string | null) => {
    setBusy(true); setError('');
    try {
      show((await setCarryoverPayoutWindow(s, e)).data);       // the stored value, not the form's
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'The dates were not saved. Try again.');
    } finally {
      setBusy(false);
    }
  };

  /** Download the Excel file, named by the server. */
  const download = async () => {
    setExporting(true); setError('');
    try {
      const r = await exportCarryoverPayout();
      const name = /filename="([^"]+)"/.exec(r.headers['content-disposition'] || '')?.[1] || 'carry-over-and-payout.xlsx';
      const url = URL.createObjectURL(r.data);                 // the file as a temporary link
      const a = document.createElement('a');
      a.href = url; a.download = name; a.click();
      URL.revokeObjectURL(url);
    } catch {
      setError('The export could not be made. Try again.');
    } finally {
      setExporting(false);
    }
  };

  // One line on where things stand, plus a coloured chip for it.
  const status = !saved ? 'Checking...'
    : saved.open ? `Open now, until ${longDate(saved.end!)}.`
    : !saved.start ? 'Closed: no dates set. The request page greys carry-over and payout out.'
    : saved.today < saved.start ? `Closed until ${longDate(saved.start)}.`
    : `Closed since ${longDate(saved.end!)}.`;
  const changed = !!saved && (start !== (saved.start || '') || end !== (saved.end || ''));

  return (
    <Paper sx={{ p: 2.5, display: 'grid', gap: 1.5 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
        <Box sx={{ flex: 1, minWidth: 220 }}>
          <Typography sx={{ fontWeight: 700 }}>Carry-over and payout requests</Typography>
          <Typography variant="body2" color="text.secondary">{status}</Typography>
        </Box>
        {saved && <Chip size="small" label={saved.open ? 'Open' : 'Closed'} color={saved.open ? 'success' : 'default'} />}
      </Box>
      {/* Both dates are days in Toronto and both are included. */}
      <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', alignItems: 'center' }}>
        <TextField label="First day" type="date" size="small" value={start} onChange={(e) => setStart(e.target.value)}
          InputLabelProps={{ shrink: true }} disabled={!saved || busy} />
        <TextField label="Last day" type="date" size="small" value={end} onChange={(e) => setEnd(e.target.value)}
          InputLabelProps={{ shrink: true }} disabled={!saved || busy} inputProps={{ min: start || undefined }} />
        <Button variant="contained" disabled={!changed || !start || !end || busy} onClick={() => save(start, end)}>Save dates</Button>
        <Button disabled={!saved?.start || busy} onClick={() => save(null, null)}>Clear</Button>
        <Box sx={{ flex: 1 }} />
        <Button variant="outlined" startIcon={<FileDownloadOutlinedIcon />} disabled={exporting} onClick={download}>
          {exporting ? 'Preparing...' : 'Export to Excel'}
        </Button>
      </Box>
      <Typography variant="caption" color="text.secondary">
        The export lists everyone's vacation, carry-over and payout days, and the carry-over and payout requests made between these dates (all of them when none are set).
      </Typography>
      {error && <Alert severity="error">{error}</Alert>}
    </Paper>
  );
}
