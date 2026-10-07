import { useCallback, useEffect, useState } from 'react';
import {
  Paper, Typography, Table, TableHead, TableRow, TableCell, TableBody, Button, Alert,
  Chip, CircularProgress, Box,
} from '@mui/material';
import { getInvites, resendInvite } from '../api/client';

/** One employee's site access record, as /admin/invites returns it. */
interface InviteRow {
  employee_id: string;
  email: string;
  name: string;
  status: string;
  group_added: boolean;
  detail: string | null;
  attempts: number;
  updated_at: string | null;
}

const STATUS_LABEL: Record<string, string> = {
  on_site: 'On site', in_tenant: 'Member', invited: 'Invited', skipped: 'Off', failed: 'Failed',
};
const STATUS_COLOR: Record<string, 'success' | 'info' | 'default' | 'error'> = {
  on_site: 'success', in_tenant: 'success', invited: 'info', skipped: 'default', failed: 'error',
};

/**
 * Site access for each employee added from the dashboards: whether they were
 * found in Microsoft 365 or invited as a guest, and added to the site members
 * group. Resend repeats the whole step (a guest who lost the email, or a
 * failure fixed since).
 *
 * @param props.processingEnabled - Disables Resend in reporting-only mode.
 * @returns The Site Invites panel.
 */
export default function SiteInvites({ processingEnabled }: { processingEnabled: boolean }) {
  const [rows, setRows] = useState<InviteRow[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [message, setMessage] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);

  /** Reload the list. */
  const load = useCallback(async () => {
    try {
      const res = await getInvites();
      setRows(res.data.invites || []);
      setEnabled(!!res.data.enabled);
    } catch {
      setMessage({ severity: 'error', text: 'Site invites could not be loaded.' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  /** Resend one employee's invite. @param row - the employee. */
  const resend = async (row: InviteRow) => {
    setBusyId(row.employee_id);
    setMessage(null);
    try {
      const res = await resendInvite(row.employee_id);
      const failed = res.data.status === 'failed';
      setMessage({ severity: failed ? 'error' : 'success', text: `${row.name}: ${res.data.detail}` });
      await load();
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setMessage({ severity: 'error', text: typeof detail === 'string' ? detail : 'Resend failed.' });
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h6" sx={{ fontWeight: 600, mb: 0.5 }}>Site invites</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Adding an employee gives them access to the SharePoint site: found in Microsoft 365, or
        invited as a guest, then added to the site members group.
      </Typography>
      {!enabled && (
        <Alert severity="info" sx={{ mb: 2 }}>
          Site invites are off (INVITES_ENABLED). IT adds new employees to the site by hand until
          they are turned on.
        </Alert>
      )}
      {message && <Alert severity={message.severity} sx={{ mb: 2 }}>{message.text}</Alert>}
      {loading ? <CircularProgress size={24} /> : rows.length === 0 ? (
        <Typography color="text.secondary">No employees added since this started.</Typography>
      ) : (
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Employee</TableCell>
                <TableCell>Status</TableCell>
                <TableCell>Detail</TableCell>
                <TableCell>Updated</TableCell>
                <TableCell />
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={r.employee_id}>
                  <TableCell>
                    {r.name}<br />
                    <Typography variant="caption" color="text.secondary">{r.email}</Typography>
                  </TableCell>
                  <TableCell>
                    <Chip size="small" label={STATUS_LABEL[r.status] ?? r.status}
                      color={STATUS_COLOR[r.status] ?? 'default'} />
                  </TableCell>
                  <TableCell>{r.detail}</TableCell>
                  <TableCell>{r.updated_at?.slice(0, 10)}</TableCell>
                  <TableCell>
                    <Button size="small" disabled={!processingEnabled || !enabled || busyId === r.employee_id}
                      onClick={() => resend(r)}>
                      Resend
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      )}
    </Paper>
  );
}
