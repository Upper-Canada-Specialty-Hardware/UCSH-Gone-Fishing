import { useCallback, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Box, Alert, Stack, Button, Typography, CircularProgress, Paper, Link,
  Dialog, DialogTitle, DialogContent, DialogActions, TextField,
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import { PROBLEM_INFO } from './employeeSetupProblems';
import { fixNegativeCarryover } from '../api/client';
import SettleRequestsDialog, { UnmovedRequest, kindLabel } from './SettleRequestsDialog';
import { formatDayRange } from '../utils/dates';

// One failing check, as the sweep reports it.
interface Problem {
  code: string;
  category: string;
  detail: string;
}

export interface EmployeeSetupSummary {
  flagged: {
    employee_id: string;
    employee_name: string;
    department: string;
    location: string;
    fails: Problem[];
    warns: Problem[];
    /** Approved requests that never moved a balance, for Settle requests (#400). */
    unmoved_requests?: UnmovedRequest[];
  }[];
  total_checked: number;
  directory_unreadable: boolean;
}

interface Props {
  setupList: EmployeeSetupSummary | null;
  loading: boolean;
  onRefresh: () => Promise<void>;
  onSelect: (employeeId: string) => void;
}

// One row as the list draws it: the person and what is wrong with their record.
interface Row {
  id: string;
  name: string;
  department: string;
  fails: Problem[];
  unmoved: UnmovedRequest[];
}

// Problems with a fix of their own on the row; every other one is fixed on the profile.
const ROW_FIXED = new Set(['approved_balance_unmoved', 'carry_over_negative']);

// The three columns at desktop width; one stacked column on a phone, so nothing scrolls sideways.
const GRID = { xs: '1fr', md: '220px minmax(0, 1fr) 210px' };

/**
 * The heading of one problem, counted where a count says more than the title.
 *
 * @param problem - The failing check.
 * @param unmoved - The person's flagged requests, for the unmoved-balance count.
 * @returns e.g. "2 approved requests never moved a balance".
 */
function problemTitle(problem: Problem, unmoved: UnmovedRequest[]): string {
  if (problem.code === 'approved_balance_unmoved' && unmoved.length > 1) {
    return `${unmoved.length} approved requests never moved a balance`;   // the count leads
  }
  return PROBLEM_INFO[problem.code]?.title || problem.code;               // unknown codes still show
}

/**
 * The line under a problem saying exactly what is wrong.
 *
 * @param problem - The failing check.
 * @param unmoved - The person's flagged requests.
 * @returns Each flagged request as "#4107 Vacation, Aug 17 to Aug 18, 2026" for an
 *   unmoved balance; the check's own detail for everything else.
 */
function problemDetail(problem: Problem, unmoved: UnmovedRequest[]): string {
  if (problem.code === 'approved_balance_unmoved' && unmoved.length > 0) {
    return unmoved
      .map((r) => `#${r.item_id} ${kindLabel(r)}${r.start_date ? `, ${formatDayRange(r.start_date, r.end_date)}` : ''}`)
      .join('; ') + '.';                                                 // one sentence listing them
  }
  return problem.detail;                                                 // the check already says what it found
}

/**
 * Everyone whose staff record will block or confuse a request, listed up front
 * rather than found one at a time.
 *
 * Each row shows the person, every problem with a line saying exactly what is
 * wrong, and a labelled action: Settle requests and Move onto vacation fix their
 * problem right here; anything else is fixed on the person's profile. Clicking
 * the name runs the single-employee check above for them.
 *
 * @param props - See {@link Props}.
 * @returns The list section.
 */
export default function EmployeeSetupList({ setupList, loading, onRefresh, onSelect }: Props) {
  const navigate = useNavigate();
  // The person whose flagged requests the settle dialog is walking through, or null.
  const [settling, setSettling] = useState<{ name: string; requests: UnmovedRequest[] } | null>(null);

  const rows = useMemo<Row[]>(
    () =>
      (setupList?.flagged || []).map((r) => ({
        id: r.employee_id,
        name: r.employee_name || `#${r.employee_id}`,
        department: r.department || '',
        fails: r.fails || [],
        unmoved: r.unmoved_requests || [],
      })),
    [setupList],
  );

  // The one-click carry-over correction, straight from the list (#407). A reason
  // is required, as for any balance edit, so the action opens a dialog.
  const [fixId, setFixId] = useState<string | null>(null);   // whose carry-over is being fixed
  const [fixName, setFixName] = useState('');                // their name, for the dialog wording
  const [fixReason, setFixReason] = useState('');            // the typed reason for the move
  const [fixBusy, setFixBusy] = useState(false);             // the fix request is in flight
  const [fixError, setFixError] = useState('');              // why the fix was refused, if it was

  /**
   * Open the carry-over fix dialog for one person, seeding its state.
   *
   * @param id - Staff id whose negative carry-over is being fixed.
   * @param name - Their display name, for the dialog wording.
   */
  const openFix = useCallback((id: string, name: string) => {
    setFixId(id);
    setFixName(name);
    setFixReason('');
    setFixError('');
  }, []);

  /**
   * Post the carry-over fix for the open person, then refresh the list.
   *
   * Moves the stored negative carry-over onto vacation with the typed reason; on
   * success the dialog closes and the list re-runs so the row clears, and on
   * failure the backend's message is shown and the dialog stays open.
   *
   * @returns A promise that resolves once the request and refresh settle.
   */
  const submitFix = async () => {
    if (!fixId || !fixReason.trim()) return;                 // guarded; the button is also disabled
    setFixBusy(true);
    setFixError('');
    try {
      await fixNegativeCarryover(fixId, fixReason.trim());   // C -> 0, V += C, history recorded
      setFixId(null);
      await onRefresh();                                     // re-run the sweep so the row clears
    } catch (err: any) {
      const detail = err.response?.data?.detail;             // the backend's 400/404/503 message
      setFixError(typeof detail === 'string' ? detail : 'The carry-over could not be corrected. Please try again.');
    } finally {
      setFixBusy(false);
    }
  };

  /**
   * Go to the person's profile on the Staff screen, where the rest is fixed.
   *
   * @param id - Their staff id; the Staff screen opens it from ?open=<id>.
   */
  const openProfile = (id: string) => navigate(`/admin/staff?open=${encodeURIComponent(id)}`);

  return (
    <Box>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 2, mb: 1 }}>
        <Typography
          component="h2"
          sx={{ fontSize: 12, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: 'text.secondary' }}
        >
          Everyone with a problem
        </Typography>
        <Button
          size="small"
          onClick={onRefresh}
          disabled={loading}
          startIcon={loading ? <CircularProgress size={14} color="inherit" /> : <RefreshIcon />}
          sx={{ flexShrink: 0 }}
        >
          Refresh
        </Button>
      </Box>

      {setupList?.directory_unreadable && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          The account list could not be read, so the identity checks were skipped. Refresh to run them again.
        </Alert>
      )}

      <Paper variant="outlined" sx={{ borderRadius: 3, overflow: 'hidden' }}>
        {/* Column headings, desktop only: on a phone each row stacks and labels itself. */}
        <Box
          sx={{
            display: { xs: 'none', md: 'grid' }, gridTemplateColumns: GRID.md, gap: 2, px: 2.5, py: 1.5,
            fontSize: 12, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase',
            color: 'text.secondary', borderBottom: 1, borderColor: 'divider',
          }}
        >
          <span>Person</span><span>Problem</span><span>Actions</span>
        </Box>

        {!setupList && loading && (
          <Box sx={{ p: 3, display: 'flex', justifyContent: 'center' }}><CircularProgress size={24} /></Box>
        )}

        {setupList && rows.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ px: 2.5, py: 3.5 }}>
            Nothing to fix. All {setupList.total_checked} staff records are set up.
          </Typography>
        )}

        {rows.map((row) => {
          const hasNegativeCarryover = row.fails.some((f) => f.code === 'carry_over_negative');
          // The profile button shows only when some problem has no fix of its own on the row.
          const needsProfile = row.fails.some((f) => !ROW_FIXED.has(f.code)) || row.fails.length === 0;
          return (
            <Box
              key={row.id}
              sx={{
                display: 'grid', gridTemplateColumns: GRID, gap: { xs: 1.25, md: 2 }, alignItems: 'center',
                px: 2.5, py: 2, borderBottom: 1, borderColor: 'divider', '&:last-of-type': { borderBottom: 0 },
              }}
            >
              {/* Person: the name runs the single-employee check for them. */}
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25, minWidth: 0 }}>
                <Link
                  component="button"
                  type="button"
                  underline="hover"
                  color="inherit"
                  onClick={() => onSelect(row.id)}
                  aria-label={`Check ${row.name}'s setup`}
                  sx={{ fontWeight: 700, fontSize: 15, textAlign: 'left', alignSelf: 'flex-start' }}
                >
                  {row.name}
                </Link>
                {row.department && (
                  <Typography variant="body2" color="text.secondary">{row.department}</Typography>
                )}
              </Box>

              {/* Problem: each one with the line saying exactly what is wrong. */}
              <Stack spacing={1} sx={{ minWidth: 0 }}>
                {row.fails.map((problem) => (
                  <Box key={problem.code} sx={{ display: 'flex', flexDirection: 'column', gap: 0.25 }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      <Box
                        component="span"
                        aria-hidden="true"
                        sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: 'error.main', flex: 'none' }}
                      />
                      <Typography variant="body2" sx={{ fontWeight: 600 }}>
                        {problemTitle(problem, row.unmoved)}
                      </Typography>
                    </Box>
                    {problemDetail(problem, row.unmoved) && (
                      <Typography variant="body2" color="text.secondary" sx={{ pl: 2, overflowWrap: 'anywhere' }}>
                        {problemDetail(problem, row.unmoved)}
                      </Typography>
                    )}
                  </Box>
                ))}
              </Stack>

              {/* Actions: labelled, the row's own fixes first, then the profile. */}
              <Stack direction={{ xs: 'row', md: 'column' }} spacing={1} useFlexGap flexWrap="wrap" alignItems="flex-start">
                {row.unmoved.length > 0 && (
                  <Button
                    variant="contained"
                    size="small"
                    onClick={() => setSettling({ name: row.name, requests: row.unmoved })}
                  >
                    Settle requests
                  </Button>
                )}
                {hasNegativeCarryover && (
                  <Button variant="outlined" size="small" onClick={() => openFix(row.id, row.name)}>
                    Move onto vacation
                  </Button>
                )}
                {needsProfile && (
                  <Button variant="outlined" size="small" color="inherit" onClick={() => openProfile(row.id)}>
                    Open profile
                  </Button>
                )}
              </Stack>
            </Box>
          );
        })}
      </Paper>

      {/* The carry-over fix dialog: a required reason, then C -> 0 and V += C (#407). */}
      <Dialog open={fixId !== null} onClose={() => !fixBusy && setFixId(null)} fullWidth maxWidth="xs">
        <DialogTitle>Move {fixName || 'this person'}'s carry-over onto vacation</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            This sets carry-over to zero and folds the shortfall into vacation. The reason is recorded
            in their change history, like any other balance edit.
          </Typography>
          {fixError && <Alert severity="error" sx={{ mb: 2 }}>{fixError}</Alert>}
          <TextField
            label="Reason" value={fixReason} onChange={(e) => setFixReason(e.target.value)}
            fullWidth multiline minRows={2} autoFocus disabled={fixBusy}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setFixId(null)} disabled={fixBusy}>Cancel</Button>
          <Button variant="contained" onClick={submitFix} disabled={fixBusy || !fixReason.trim()}>
            {fixBusy ? <CircularProgress size={20} /> : 'Move onto vacation'}
          </Button>
        </DialogActions>
      </Dialog>

      <SettleRequestsDialog
        personName={settling?.name || ''}
        requests={settling ? settling.requests : null}
        // Anything saved clears flags, so re-run the sweep to drop settled people.
        onClose={(settled) => { setSettling(null); if (settled) onRefresh(); }}
      />
    </Box>
  );
}
