import { useState } from 'react';
import { Box, Button, Paper, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { VacationCoverRequest } from '../../api/client';
import RejectDialog from '../RejectDialog';
import { riseAt, Swap } from '../Motion';

interface Props {
  /** The pending requests whose managers are all off (from the vacation-cover endpoint). */
  requests: VacationCoverRequest[];
  /** False in reporting-only mode: the approve/reject buttons are disabled. */
  processingEnabled: boolean;
  /** The `${type}-${id}` key of a request whose action is running, or null. */
  actionLoading: string | null;
  /** The in-flight decision's direction (#485): true approving, false rejecting, null idle; applies to the busy row. */
  decidingYes?: boolean | null;
  /** Approve a request. @param type - leave/overtime/carryover-payout. @param id - The request id. */
  onApprove: (type: string, id: string) => void;
  /** Reject a request with a reason (#433). @param type - leave/overtime/carryover-payout. @param id - The request id. @param reason - Why it was turned down. */
  onReject: (type: string, id: string, reason: string) => void;
}

/**
 * The "Managers on vacation" screen: pending requests no manager can approve
 * because every manager on the request is off, shown with who the managers are
 * and when each is back, so an admin can step in and approve or reject here. A
 * request leaves the list on its own once a manager returns (#370).
 *
 * Cards stack and wrap rather than scroll sideways, per the admin table rules.
 *
 * @param props - See {@link Props}.
 * @returns The screen.
 */
export default function ManagersOnVacation({ requests, processingEnabled, actionLoading, decidingYes = null, onApprove, onReject }: Props) {
  const theme = useTheme();
  const [rejectTarget, setRejectTarget] = useState<VacationCoverRequest | null>(null);   // the card awaiting a reason (#433)

  if (requests.length === 0) {
    return (
      <Paper sx={{ p: 2.5 }}>
        <Typography sx={{ fontWeight: 700, mb: 0.5 }}>Managers on vacation</Typography>
        <Typography color="text.secondary">
          No request is stuck. Every pending request has a manager who can approve it.
        </Typography>
      </Paper>
    );
  }

  return (
    <Paper>
      <Box sx={{ px: 2.5, py: 1.75, borderBottom: 1, borderColor: 'divider' }}>
        <Typography sx={{ fontWeight: 700 }}>Managers on vacation</Typography>
        <Typography variant="body2" color="text.secondary">
          Every manager on these requests is off, so no one can approve them. Step in below, or leave
          them for the manager who is back soonest.
        </Typography>
      </Box>

      <Box sx={{ display: 'grid', gap: 1.5, p: { xs: 1.5, sm: 2.5 } }}>
        {requests.map((r, i) => {
          const busy = actionLoading === `${r.request_type}-${r.id}`;
          return (
            <Box
              key={`${r.request_type}-${r.id}`}
              sx={{
                display: 'grid', gap: 1.25, p: 2, borderRadius: `${theme.shape.borderRadius}px`,
                border: 1, borderColor: 'divider', bgcolor: 'background.paper',
                // Two columns on wider screens (details then actions); one, stacked, on phones.
                gridTemplateColumns: { xs: '1fr', sm: 'minmax(0, 1fr) auto' },
                alignItems: { sm: 'center' },
                ...riseAt(i, { step: 40 }),
              }}
            >
              <Box sx={{ display: 'grid', gap: 0.5, minWidth: 0 }}>
                <Typography sx={{ fontWeight: 600 }}>{r.employee_name}</Typography>
                <Typography variant="body2" color="text.secondary">
                  {r.what}{r.when ? ` - ${r.when}` : ''}
                </Typography>
                {/* Who is off and when each is back, so the admin knows how long the wait is. */}
                <Typography variant="body2" sx={{ color: 'text.primary' }}>
                  {r.managers.map((m) => `${m.name} (back ${m.back})`).join(', ')}
                </Typography>
              </Box>
              <Box sx={{ display: 'flex', gap: 1, justifyContent: { xs: 'flex-start', sm: 'flex-end' } }}>
                <Button
                  size="small" variant="contained" color="success"
                  disabled={busy || !processingEnabled}
                  onClick={() => onApprove(r.request_type, r.id)}
                >
                  {/* While this request's approval runs the label reads "Approving..." (#485). */}
                  <Swap k={busy && decidingYes === true ? 'busy' : 'idle'}>
                    {busy && decidingYes === true ? 'Approving...' : 'Approve'}
                  </Swap>
                </Button>
                <Button
                  size="small" variant="outlined" color="error"
                  disabled={busy || !processingEnabled}
                  onClick={() => setRejectTarget(r)}
                >
                  {/* While this request's rejection runs the label reads "Rejecting..." (#485). */}
                  <Swap k={busy && decidingYes === false ? 'busy' : 'idle'}>
                    {busy && decidingYes === false ? 'Rejecting...' : 'Reject'}
                  </Swap>
                </Button>
              </Box>
            </Box>
          );
        })}
      </Box>

      {/* Every reject asks for a reason first (#433). */}
      <RejectDialog
        open={rejectTarget !== null}
        personName={rejectTarget?.employee_name}
        requestLabel={rejectTarget?.what}
        busy={rejectTarget !== null && actionLoading === `${rejectTarget.request_type}-${rejectTarget.id}`}
        onCancel={() => setRejectTarget(null)}
        onConfirm={(reason) => { const r = rejectTarget; setRejectTarget(null); if (r) onReject(r.request_type, r.id, reason); }}
      />
    </Paper>
  );
}
