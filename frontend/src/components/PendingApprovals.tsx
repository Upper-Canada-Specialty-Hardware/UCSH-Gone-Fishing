import { Card, CardContent, Typography, Button, Box, Chip, Stack } from '@mui/material';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import CancelIcon from '@mui/icons-material/Cancel';
import { getDescription } from './dataGridDefaults';

interface Balances {
  vacation_balance: number;
  sick_balance: number;
  overtime: number;
  carryover: number;
  [key: string]: number;
}

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

const balanceFields: { key: string; label: string }[] = [
  { key: 'vacation_balance', label: 'Vacation' },
  { key: 'sick_balance', label: 'Sick' },
  { key: 'overtime', label: 'Make-Up' },
  { key: 'carryover', label: 'Carry Over' },
];

function BalanceRow({ label, balances, compare }: {
  label: string;
  balances: Balances;
  compare?: Balances;
}) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap', mt: 0.5 }}>
      <Typography variant="caption" color="text.secondary" sx={{ minWidth: 80 }}>
        {label}
      </Typography>
      {balanceFields.map(({ key, label: fieldLabel }) => {
        const val = balances[key] ?? 0;
        const cmp = compare?.[key];
        let color: string = 'text.secondary';
        let fontWeight = 400;
        if (cmp !== undefined) {
          if (val < cmp) { color = 'error.main'; fontWeight = 600; }
          else if (val > cmp) { color = 'success.main'; fontWeight = 600; }
        }
        return (
          <Typography key={key} variant="caption" sx={{ color, fontWeight }}>
            {fieldLabel}: {val}
          </Typography>
        );
      })}
    </Box>
  );
}

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
        const name = item.employee_name || (item.Title || '').split(' /// ')[0] || item.SubmitterName || 'Unknown';
        const description = getDescription(item);
        const key = `${item.request_type}-${item.id}`;
        const isLoading = actionLoading === key;

        return (
          <Card key={key} variant="outlined">
            <CardContent>
              <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                <Box>
                  <Typography variant="h6" gutterBottom>
                    {name}
                  </Typography>
                  {item.managers && (
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 0.5 }}>
                      Manager(s): {item.managers}
                    </Typography>
                  )}
                  <Chip label={typeLabel[item.request_type] || item.request_type} size="small" sx={{ mr: 1 }} />
                  {item.LeaveType && <Chip label={item.LeaveType} size="small" variant="outlined" sx={{ mr: 1 }} />}
                  {description && (
                    <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, fontStyle: 'italic' }}>
                      {description}
                    </Typography>
                  )}
                  <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                    {item.StartDate}
                    {item.EndDate && item.EndDate !== item.StartDate && ` — ${item.EndDate}`}
                    {item.Days != null && ` | ${item.Days} days`}
                    {item.Hours != null && ` | ${item.Hours} hours`}
                  </Typography>
                  {item.current_balances && (
                    <Box sx={{ mt: 1.5, pt: 1, borderTop: '1px solid', borderColor: 'divider' }}>
                      <BalanceRow label="Current:" balances={item.current_balances} />
                      {item.balance_unchanged ? (
                        <Typography variant="caption" color="text.secondary" fontStyle="italic" sx={{ mt: 0.5, display: 'block' }}>
                          No balance change
                        </Typography>
                      ) : item.projected_balances ? (
                        <BalanceRow label="If Approved:" balances={item.projected_balances} compare={item.current_balances} />
                      ) : null}
                    </Box>
                  )}
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
