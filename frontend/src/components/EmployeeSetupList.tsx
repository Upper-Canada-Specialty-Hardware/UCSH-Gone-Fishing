import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Box, Alert, Stack, Button, Typography, CircularProgress, Paper, Link, LinearProgress, Tooltip,
  Dialog, DialogTitle, DialogContent, DialogActions, TextField,
} from '@mui/material';
import { PROBLEM_INFO } from './employeeSetupProblems';
import SettleRequestsDialog, { UnmovedRequest, kindLabel } from './SettleRequestsDialog';
import { formatDayRange } from '../utils/dates';
import { SetupScope } from '../api/client';

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
  /** A fresh read of the list is in flight; a thin bar shows it. */
  loading: boolean;
  /** Re-read the list; called after a fix here, and by Try again on a failed read. */
  onRefresh: () => Promise<void>;
  onSelect: (employeeId: string) => void;
  /** Show a short message at the bottom of the screen, like the design's toast. */
  onNotify: (message: string) => void;
  /** 'admin' (default), or 'team' on the manager dashboard (#463): settles go to the team routes. */
  scope?: SetupScope;
  /** Open a person's profile; when absent, the admin Staff screen opens it. */
  onOpenProfile?: (employeeId: string) => void;
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
const ROW_FIXED = new Set(['approved_balance_unmoved']);

/**
 * One request's key, unique across the three request kinds.
 *
 * @param r - A flagged request.
 * @returns e.g. "leave:4107".
 */
const requestKey = (r: UnmovedRequest) => `${r.kind}:${r.item_id}`;

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
 * wrong, and a labelled action: Settle requests fixes its
 * problem right here; anything else is fixed on the person's profile. Clicking
 * the name runs the single-employee check above for them.
 *
 * @param props - See {@link Props}.
 * @returns The list section.
 */
export default function EmployeeSetupList({
  setupList, loading, onRefresh, onSelect, onNotify, scope = 'admin', onOpenProfile,
}: Props) {
  const navigate = useNavigate();
  // The person whose flagged requests the settle dialog is walking through, or null.
  const [settling, setSettling] = useState<{ name: string; requests: UnmovedRequest[] } | null>(null);

  // Fixes made here, hidden at once so a row drops the moment it is fixed
  // rather than when the re-read lands. Cleared by every fresh read, which by
  // then reflects the fix itself.
  const [settledKeys, setSettledKeys] = useState<Set<string>>(new Set());   // requests settled here
  useEffect(() => {
    setSettledKeys(new Set());                                // the fresh read is the truth now
  }, [setupList]);

  const rows = useMemo<Row[]>(() => {
    const out: Row[] = [];
    for (const r of setupList?.flagged || []) {
      const unmoved = (r.unmoved_requests || []).filter((q) => !settledKeys.has(requestKey(q)));
      const allFails = r.fails || [];
      const fails = allFails.filter((f) =>
        !(f.code === 'approved_balance_unmoved' && unmoved.length === 0));             // every request settled
      if (allFails.length > 0 && fails.length === 0) continue;  // everything fixed here: drop the row
      out.push({
        id: r.employee_id,
        name: r.employee_name || `#${r.employee_id}`,
        department: r.department || '',
        fails,
        unmoved,
      });
    }
    return out;
  }, [setupList, settledKeys]);

  /**
   * Close the settle dialog: drop what was saved, say so, and re-read the list.
   *
   * @param name - The person whose requests were walked through.
   * @param all - Every request the dialog was opened with.
   * @param settled - The ones saved; empty when the dialog was cancelled.
   */
  const closeSettle = (name: string, all: UnmovedRequest[], settled: UnmovedRequest[]) => {
    setSettling(null);
    if (settled.length === 0) return;                         // nothing saved, nothing changes
    setSettledKeys((prev) => {
      const next = new Set(prev);
      settled.forEach((q) => next.add(requestKey(q)));        // hide them now
      return next;
    });
    onNotify(settled.length === all.length
      ? `${name} is settled and off the list.`                // the design's wording
      : 'Saved. Skipped requests stay on the list.');
    void onRefresh();                                         // re-read, so the other checks are current too
  };

  /**
   * Go to the person's profile, where the rest is fixed: the caller's own way
   * when it has one (the manager's Team tab), else the admin Staff screen.
   *
   * @param id - Their staff id; the Staff screen opens it from ?open=<id>.
   */
  const openProfile = (id: string) => (
    onOpenProfile ? onOpenProfile(id) : navigate(`/admin/staff?open=${encodeURIComponent(id)}`)
  );

  return (
    <Box>
      {/* No Refresh button: the list re-reads itself after a fix and whenever a staff record changes. */}
      <Typography
        component="h2"
        sx={{ fontSize: 12, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: 'text.secondary', mb: 1 }}
      >
        Everyone with a problem
      </Typography>

      {setupList?.directory_unreadable && (
        <Alert
          severity="warning"
          sx={{ mb: 2 }}
          // A failed read is the one case with a manual retry.
          action={<Button color="inherit" size="small" onClick={() => void onRefresh()} disabled={loading}>Try again</Button>}
        >
          The account list could not be read, so the identity checks were skipped.
        </Alert>
      )}

      <Paper variant="outlined" sx={{ borderRadius: 3, overflow: 'hidden', position: 'relative' }}>
        {/* A thin bar while the list is re-read; the rows stay readable underneath. */}
        {loading && setupList && (
          <LinearProgress
            aria-label="Updating the list"
            sx={{ position: 'absolute', top: 0, left: 0, right: 0, height: 2 }}
          />
        )}
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
            {scope === 'team'
              ? `Nothing to fix. All ${setupList.total_checked} people on your team are set up.`
              : `Nothing to fix. All ${setupList.total_checked} staff records are set up.`}
          </Typography>
        )}

        {rows.map((row) => {
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
                      {/* The red dot explains itself on hover, and to a screen reader. */}
                      <Tooltip title="Needs fixing: this can block or confuse a request." arrow>
                        <Box
                          component="span"
                          role="img"
                          aria-label="Needs fixing"
                          sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: 'error.main', flex: 'none' }}
                        />
                      </Tooltip>
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

      <SettleRequestsDialog
        personName={settling?.name || ''}
        requests={settling ? settling.requests : null}
        // Saved requests drop off at once, a toast says so, and the list re-reads.
        onClose={(settled) => settling && closeSettle(settling.name, settling.requests, settled)}
        scope={scope}
      />
    </Box>
  );
}
