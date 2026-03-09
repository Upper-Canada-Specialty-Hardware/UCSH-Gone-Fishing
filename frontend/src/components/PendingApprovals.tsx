import { Card, CardContent, Typography, Button, Box, Chip, Stack } from '@mui/material';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import CancelIcon from '@mui/icons-material/Cancel';

interface Props {
  pending: any[];
  processingEnabled: boolean;
  onApprove: (type: string, id: string) => void;
  onReject: (type: string, id: string) => void;
  actionLoading?: string | null;
}

const typeLabel: Record<string, string> = {
  leave: 'Leave Request',
  overtime: 'Overtime Request',
  'carryover-payout': 'Carry Over / Payout',
};

export default function PendingApprovals({ pending, processingEnabled, onApprove, onReject, actionLoading }: Props) {
  if (pending.length === 0) {
    return (
      <Typography color="text.secondary" sx={{ py: 4, textAlign: 'center' }}>
        No pending approvals.
      </Typography>
    );
  }

  return (
    <Stack spacing={2}>
      {pending.map((item) => {
        const name = (item.Title || '').split(' /// ')[0];
        const key = `${item.request_type}-${item.id}`;
        const isLoading = actionLoading === key;

        return (
          <Card key={key} variant="outlined">
            <CardContent>
              <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                <Box>
                  <Typography variant="h6" gutterBottom>
                    {name || item.SubmitterName || 'Unknown'}
                  </Typography>
                  <Chip label={typeLabel[item.request_type] || item.request_type} size="small" sx={{ mr: 1 }} />
                  {item.LeaveType && <Chip label={item.LeaveType} size="small" variant="outlined" sx={{ mr: 1 }} />}
                  <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                    {item.StartDate}
                    {item.EndDate && item.EndDate !== item.StartDate && ` — ${item.EndDate}`}
                    {item.Days != null && ` | ${item.Days} days`}
                    {item.Hours != null && ` | ${item.Hours} hours`}
                  </Typography>
                </Box>
                <Box sx={{ display: 'flex', gap: 1, flexShrink: 0 }}>
                  <Button
                    variant="contained"
                    color="success"
                    size="small"
                    startIcon={<CheckCircleIcon />}
                    disabled={!processingEnabled || isLoading}
                    onClick={() => onApprove(item.request_type, item.id)}
                    title={!processingEnabled ? 'Processing is currently disabled' : ''}
                  >
                    Approve
                  </Button>
                  <Button
                    variant="contained"
                    color="error"
                    size="small"
                    startIcon={<CancelIcon />}
                    disabled={!processingEnabled || isLoading}
                    onClick={() => onReject(item.request_type, item.id)}
                    title={!processingEnabled ? 'Processing is currently disabled' : ''}
                  >
                    Reject
                  </Button>
                </Box>
              </Box>
            </CardContent>
          </Card>
        );
      })}
    </Stack>
  );
}
