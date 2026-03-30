import {
  Dialog, DialogTitle, DialogContent, IconButton,
  Box, Typography, Table, TableHead, TableBody, TableRow, TableCell,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';

const BALANCE_LABELS: Record<string, string> = {
  CurrentOvertimeBalance: 'Make-Up',
  CurrentVacationBalance: 'Vacation',
  CurrentSickDayBalance: 'Sick',
  CarryOver: 'Carry Over',
  Payout: 'Payout',
};

interface Props {
  open: boolean;
  onClose: () => void;
  auditLog: string;
}

export default function AuditTrailDialog({ open, onClose, auditLog }: Props) {
  let entries: any[];
  try {
    entries = JSON.parse(auditLog);
  } catch {
    entries = [];
  }

  if (!Array.isArray(entries)) entries = [];

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        Balance Audit Trail
        <IconButton onClick={onClose} size="small">
          <CloseIcon />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {entries.length === 0 ? (
          <Typography color="text.secondary">No audit data available.</Typography>
        ) : (
          entries.map((entry: any, i: number) => (
            <Box key={i} sx={{ mb: i < entries.length - 1 ? 2 : 0 }}>
              <Typography variant="subtitle2" sx={{ mb: 0.5, fontWeight: 600 }}>
                {entry.action === 'approve' ? 'Approved' : entry.action === 'refund' ? 'Refunded' : entry.action}
                {' — '}
                {entry.timestamp}
              </Typography>
              <Table size="small" sx={{ '& td, & th': { py: 0.5, px: 1 } }}>
                <TableHead>
                  <TableRow>
                    <TableCell sx={{ fontWeight: 600 }}>Operation</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>Changes</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>Detail</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {(entry.steps || []).map((step: any, j: number) => (
                    <TableRow key={j}>
                      <TableCell>{step.operation}</TableCell>
                      <TableCell>
                        {Object.keys(step.before || {}).map((key) => {
                          const label = BALANCE_LABELS[key] || key;
                          const bv = step.before[key];
                          const av = step.after?.[key];
                          return (
                            <Box key={key} component="span" sx={{ display: 'block', whiteSpace: 'nowrap' }}>
                              {label}: {bv} &rarr; {av}
                            </Box>
                          );
                        })}
                      </TableCell>
                      <TableCell>{step.detail || ''}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          ))
        )}
      </DialogContent>
    </Dialog>
  );
}
