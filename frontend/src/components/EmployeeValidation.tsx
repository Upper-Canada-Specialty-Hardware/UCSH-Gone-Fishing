import { useMemo, useState, type ReactNode } from 'react';
import {
  Box, Autocomplete, TextField, Button, CircularProgress, Alert,
  Typography, Paper, Divider, Collapse, Stack, Chip,
  Table, TableHead, TableBody, TableRow, TableCell,
} from '@mui/material';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import ExpandLessIcon from '@mui/icons-material/ExpandLess';
import { validateEmployee } from '../api/client';

// Matches the backend employee_validation.build_validation_report shape.
// What a check measured and what it was held against. `comparison` is
// 'reported' for rows that carry a number but no expectation, which are shown
// but never graded.
interface Measure {
  label: string;
  actual: number | null;
  expected: number | number[] | null;
  comparison: 'equals' | 'at_least' | 'at_most' | 'within' | 'reported';
}
interface Check {
  code: string;
  category: string;
  status: 'pass' | 'warn' | 'fail';
  detail: string;
  projected: Record<string, number> | null;
  measure: Measure | null;
}
interface Report {
  employee_id: string;
  employee_name: string;
  overall: 'pass' | 'warn' | 'fail';
  measurements: { total: number; within_range: number };
  current_balances: Record<string, number | null>;
  checks: Check[];
}

// Plain-language title + fix for each problem code, written for a non-technical
// HR admin. The backend's `detail` (which carries the specifics) is shown under
// the title; this is the human "what it means / what to do".
const PROBLEM_INFO: Record<string, { title: string; fix: string }> = {
  employee_record: {
    title: 'Employee record not found',
    fix: 'Confirm this person has a row in the Staff Directory.',
  },
  identity_roundtrip: {
    title: 'Not linked to Microsoft 365',
    fix: 'Make sure the email on the Staff Directory record matches their Microsoft 365 account, so the system can tell who submitted a request.',
  },
  supervisor_set: {
    title: 'No supervisor assigned',
    fix: 'Set their supervisor in the Staff Directory so their requests have someone to approve them.',
  },
  supervisor_resolves: {
    title: 'A supervisor does not match a real employee',
    fix: 'Re-pick their supervisor from the directory so approvals reach the right person.',
  },
  manager_reachable: {
    title: 'A supervisor has no email address',
    fix: 'Add an email address for the supervisor so approval emails can reach them.',
  },
  location_province: {
    title: 'Office location not recognized',
    fix: 'Choose a valid office location so vacation and leave days calculate correctly.',
  },
  holidays_load: {
    title: 'No holiday calendar for their province',
    fix: 'Add holidays for their province, otherwise every weekday counts as a workday.',
  },
  balances_numeric: {
    title: 'A balance value is not a number',
    fix: 'Correct the balance value on their Staff Directory record.',
  },
  identity_unique_name: {
    title: 'Someone else has the same name',
    fix: 'Two staff records share this name. Requests are matched back to a person by name, so one of the records needs a distinguishing name before their requests can be routed reliably.',
  },
  manager_m365_match: {
    title: 'A supervisor’s email does not match a Microsoft 365 account',
    fix: 'Correct the supervisor’s email so it matches their Microsoft 365 account. Until it does, their requests cannot record a manager — nobody is asked to approve, and the request stays hidden from the dashboards.',
  },
  supervisor_not_self: {
    title: 'They are listed as their own supervisor',
    fix: 'Change their supervisor in the Staff Directory, otherwise their requests are sent to them to approve.',
  },
  holidays_current_year: {
    title: 'No holidays for the current year',
    fix: 'Add this year’s holidays for their province. Without them, holidays are counted as ordinary workdays and leave is over-deducted.',
  },
  balances_in_range: {
    title: 'A balance value looks wrong',
    fix: 'Check the balance against their history — a value outside the expected range usually means an approval only half-applied.',
  },
  entitlements_set: {
    title: 'A yearly entitlement is missing',
    fix: 'Set their yearly vacation and sick entitlements in the Staff Directory so their annual grant can be worked out.',
  },
  requests_missing_days: {
    title: 'A request never got its days worked out',
    fix: 'Reprocess the request from Stuck Requests. Until then it is hidden from every dashboard and nobody can action it.',
  },
  requests_missing_manager: {
    title: 'A request never got a manager',
    fix: 'Reprocess the request from Stuck Requests. Nobody was asked to approve it, and it does not appear on any dashboard.',
  },
  requests_not_notified: {
    title: 'A request was never sent to its manager',
    fix: 'The request has a manager but the approval was never sent. Reprocess it from Stuck Requests to send it.',
  },
  requests_auto_rejected: {
    title: 'The system recently rejected a request on its own',
    fix: 'Read the reason shown above. If it is wrong, the employee can submit again once the cause is cleared.',
  },
  requests_approved_dates: {
    title: 'Dates already booked',
    fix: 'These approved requests hold their dates. A new request covering the same days will be refused at approval.',
  },
};

/**
 * Render a measurement as "actual / expected" for the technical breakdown.
 *
 * @param measure - The measurement carried by a check, if it has one.
 * @returns A short comparison string, or an empty string when there is nothing
 *   meaningful to compare against.
 */
function measureSummary(measure: Measure | null): string {
  if (!measure || measure.actual == null) return '';
  if (measure.comparison === 'reported' || measure.expected == null) return `${measure.actual}`;
  const expected = Array.isArray(measure.expected)
    ? `${measure.expected[0]}–${measure.expected[1]}`
    : measure.expected;
  const operator = measure.comparison === 'at_least' ? '≥'
    : measure.comparison === 'at_most' ? '≤' : '';
  return `${measure.actual} / ${operator}${expected}`;
}

/**
 * True when a check could not be run at all, as opposed to running and failing.
 *
 * The backend marks these by carrying a measurement with nothing in it: the
 * 'reported' comparison and a null reading. They must not be dressed up in the
 * wording for a misconfigured record, because the record may be fine — the
 * lookup behind the check is what failed.
 *
 * @param check - One row of the report.
 * @returns Whether the row represents an unavailable reading.
 */
function isUnavailable(check: Check): boolean {
  return check.measure?.comparison === 'reported' && check.measure?.actual == null;
}

/**
 * Resolve the heading and remedy shown for a problem row.
 *
 * @param check - One row of the report.
 * @returns The title and fix wording to display.
 */
function problemInfo(check: Check): { title: string; fix: string } {
  if (isUnavailable(check)) {
    return {
      title: 'This check could not be run',
      fix: 'A lookup this check depends on could not be read. Run the check again; if it keeps happening the Microsoft 365 connection is the place to look.',
    };
  }
  return PROBLEM_INFO[check.code] || {
    title: 'Setup issue',
    fix: 'Review this record in the Staff Directory.',
  };
}

const POT_LABELS: Record<string, string> = {
  CurrentVacationBalance: 'Vacation',
  CurrentSickDayBalance: 'Sick',
  CurrentOvertimeBalance: 'Make-Up',
  CarryOver: 'Carry Over',
  Payout: 'Payout',
};

// The request types shown in the "what would this do" preview, in a sensible order.
const PREVIEW_ROWS = [
  { code: 'sim_vacation', label: 'Vacation (1 day)' },
  { code: 'sim_sick', label: 'Sick or personal (1 day)' },
  { code: 'sim_half_day', label: 'Half day (0.5 day)' },
  { code: 'sim_bereavement', label: 'Bereavement' },
  { code: 'sim_jury_duty', label: 'Jury duty' },
  { code: 'sim_next_year_vacation', label: 'Vacation, next year (1 day)' },
  { code: 'sim_overtime', label: 'Overtime (8 hours)' },
  { code: 'sim_carry_over', label: 'Carry over (1 day)' },
  { code: 'sim_payout', label: 'Payout (1 day)' },
];

// Technical-details grouping (for the one admin who wants the raw checks).
const CATEGORY_ORDER = ['identity', 'supervisor', 'location', 'balances', 'simulation', 'requests'];
const CATEGORY_LABELS: Record<string, string> = {
  identity: 'Identity',
  supervisor: 'Supervisor',
  location: 'Location & holidays',
  balances: 'Balances',
  simulation: 'Request simulations',
  requests: 'Requests already in flight',
};
const STATUS: Record<Check['status'], { color: 'success' | 'warning' | 'error'; label: string }> = {
  pass: { color: 'success', label: 'OK' },
  warn: { color: 'warning', label: 'REVIEW' },
  fail: { color: 'error', label: 'FIX' },
};

function byCode(report: Report, code: string): Check | undefined {
  return report.checks.find((c) => c.code === code);
}

function previewOutcome(check: Check | undefined, current: Record<string, number | null>): string {
  if (!check) return '';
  if (check.status === 'fail') return 'Could not simulate';
  if (check.projected) {
    const changes = Object.entries(check.projected)
      .filter(([k, v]) => current[k] != null && v !== current[k])
      .map(([k, v]) => `${POT_LABELS[k] || k} ${current[k]} → ${v}`);
    return changes.length ? changes.join(', ') : 'No balance change';
  }
  if (check.code === 'sim_carry_over' || check.code === 'sim_payout') {
    return 'Would be declined (not enough vacation)';
  }
  return 'No balance change';
}

function ToggleSection({ title, open, onToggle, children }: {
  title: string; open: boolean; onToggle: () => void; children: ReactNode;
}) {
  return (
    <Box sx={{ mb: 0.5 }}>
      <Button
        onClick={onToggle}
        size="small"
        endIcon={open ? <ExpandLessIcon /> : <ExpandMoreIcon />}
        sx={{ textTransform: 'none', color: 'text.primary', fontWeight: 600, px: 0.5 }}
      >
        {title}
      </Button>
      <Collapse in={open}>
        <Box sx={{ pt: 1, pb: 1.5, px: 0.5 }}>{children}</Box>
      </Collapse>
    </Box>
  );
}

interface Props {
  employees: any[];
}

export default function EmployeeValidation({ employees }: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState('');
  const [previewOpen, setPreviewOpen] = useState(false);
  const [techOpen, setTechOpen] = useState(false);

  const run = async () => {
    if (!selectedId) return;
    setLoading(true);
    setError('');
    setReport(null);
    try {
      const res = await validateEmployee(selectedId);
      setReport(res.data);
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'The setup check could not be run. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  // Only real setup checks are "problems". Simulations are previews: a carryover
  // that would be declined is expected behavior, not something to fix. The one
  // exception is a simulation that RAISED (status 'fail') - that is a genuine
  // engine error on this employee's data, so surface it too; otherwise the
  // verdict shows green while the backend `overall` is already 'fail'. Fails come
  // before warns so must-fix items sort above review items.
  const problems = useMemo(() => {
    const items = (report?.checks || []).filter(
      (c) => c.status !== 'pass' && (c.category !== 'simulation' || c.status === 'fail'),
    );
    return [...items.filter((c) => c.status === 'fail'), ...items.filter((c) => c.status === 'warn')];
  }, [report]);
  const realFails = useMemo(() => problems.filter((c) => c.status === 'fail'), [problems]);
  const reviewWarns = useMemo(() => problems.filter((c) => c.status === 'warn'), [problems]);

  // Technical section grouping, resilient to categories the UI doesn't know yet.
  const grouped = useMemo(() => {
    const g: Record<string, Check[]> = {};
    (report?.checks || []).forEach((c) => {
      if (!g[c.category]) g[c.category] = [];
      g[c.category].push(c);
    });
    return g;
  }, [report]);
  const orderedCategories = useMemo(() => [
    ...CATEGORY_ORDER.filter((cat) => grouped[cat]?.length),
    ...Object.keys(grouped).filter((cat) => !CATEGORY_ORDER.includes(cat)),
  ], [grouped]);

  const name = report ? (report.employee_name || `#${report.employee_id}`) : '';
  let verdict: { severity: 'success' | 'warning' | 'error'; headline: string; sub: string } | null = null;
  if (report) {
    if (realFails.length > 0) {
      verdict = {
        severity: 'error',
        headline: `${name} has ${realFails.length} issue${realFails.length === 1 ? '' : 's'} to fix.`,
        sub: `Their requests will not work correctly until ${realFails.length === 1 ? 'it is' : 'they are'} fixed.`
          + (reviewWarns.length ? ` (${reviewWarns.length} more to review.)` : ''),
      };
    } else if (reviewWarns.length > 0) {
      verdict = {
        severity: 'warning',
        headline: `${name} is set up, with ${reviewWarns.length} thing${reviewWarns.length === 1 ? '' : 's'} to review.`,
        sub: 'These will not block requests, but are worth a look below.',
      };
    } else {
      // Deliberately reports what was measured rather than promising that every
      // request will work — the promise was what made a green result misleading
      // while a stalled request was blocking the employee.
      verdict = {
        severity: 'success',
        headline: `${name} is fully set up.`,
        sub: `All ${report.measurements.total} checks on their record and their existing requests are in range.`,
      };
    }
  }

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Check whether an employee is set up correctly for leave, overtime, and payout requests.
        This reads their Staff Directory record only. It never creates a request or notifies anyone.
      </Typography>

      <Box sx={{ display: 'flex', gap: 2, mb: 3, alignItems: 'center' }}>
        <Autocomplete
          options={employees}
          disabled={loading}
          getOptionLabel={(opt: any) => `${opt.name} — ${opt.department}`}
          onChange={(_, val) => {
            setSelectedId(val?.id || null);
            setReport(null);
            setError('');
          }}
          renderInput={(params) => <TextField {...params} label="Select Employee" />}
          sx={{ flex: 1, maxWidth: 420 }}
        />
        <Button
          variant="contained"
          onClick={run}
          disabled={!selectedId || loading}
          startIcon={loading ? <CircularProgress size={16} color="inherit" /> : undefined}
        >
          Check setup
        </Button>
      </Box>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {report && verdict && (
        <Box>
          <Alert severity={verdict.severity} sx={{ mb: 2 }}>
            <Typography variant="subtitle1" sx={{ fontWeight: 600, lineHeight: 1.35 }}>
              {verdict.headline}
            </Typography>
            <Typography variant="body2">{verdict.sub}</Typography>
          </Alert>

          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
            {report.measurements.within_range} of {report.measurements.total} measurements within
            the expected range.
          </Typography>

          {problems.length > 0 && (
            <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
              <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1.5 }}>
                What to fix
              </Typography>
              <Stack spacing={2} divider={<Divider flexItem />}>
                {problems.map((c) => {
                  const info = problemInfo(c);
                  const isFail = c.status === 'fail';
                  return (
                    <Box key={c.code} sx={{ display: 'flex', gap: 1.5, alignItems: 'flex-start' }}>
                      <Box sx={{ color: isFail ? 'error.main' : 'warning.main', mt: '2px', display: 'flex' }}>
                        {isFail ? <ErrorOutlineIcon fontSize="small" /> : <WarningAmberIcon fontSize="small" />}
                      </Box>
                      <Box>
                        <Typography variant="body2" sx={{ fontWeight: 600 }}>{info.title}</Typography>
                        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.25 }}>
                          {c.detail}
                        </Typography>
                        <Typography variant="body2" sx={{ mt: 0.5 }}>
                          <Box component="span" sx={{ fontWeight: 600 }}>Fix:</Box> {info.fix}
                        </Typography>
                      </Box>
                    </Box>
                  );
                })}
              </Stack>
            </Paper>
          )}

          <Divider sx={{ mb: 0.5 }} />

          <ToggleSection
            title="What each request would do"
            open={previewOpen}
            onToggle={() => setPreviewOpen((o) => !o)}
          >
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell sx={{ border: 0, py: 0.5, width: '42%', color: 'text.secondary', fontWeight: 600 }}>
                    Request
                  </TableCell>
                  <TableCell sx={{ border: 0, py: 0.5, color: 'text.secondary', fontWeight: 600 }}>
                    Effect on balances
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {PREVIEW_ROWS.map((row) => {
                  const c = byCode(report, row.code);
                  if (!c) return null;
                  const outcome = previewOutcome(c, report.current_balances);
                  const muted = outcome === 'No balance change';
                  return (
                    <TableRow key={row.code}>
                      <TableCell sx={{ border: 0, py: 0.5, width: '42%' }}>{row.label}</TableCell>
                      <TableCell sx={{ border: 0, py: 0.5, color: muted ? 'text.secondary' : 'text.primary' }}>
                        {outcome}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </ToggleSection>

          <ToggleSection
            title="Technical details"
            open={techOpen}
            onToggle={() => setTechOpen((o) => !o)}
          >
            {orderedCategories.map((cat) => (
              <Box key={cat} sx={{ mb: 1.5 }}>
                <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 0.75 }}>
                  {CATEGORY_LABELS[cat] || cat}
                </Typography>
                <Stack spacing={0.75}>
                  {grouped[cat].map((c) => {
                    const st = STATUS[c.status] || STATUS.warn;
                    const summary = measureSummary(c.measure);
                    return (
                      <Box key={c.code} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
                        <Chip
                          size="small"
                          color={st.color}
                          label={st.label}
                          sx={{ minWidth: 60, height: 20, fontSize: 11, fontWeight: 600 }}
                        />
                        <Box>
                          <Typography variant="body2" color="text.secondary">{c.detail}</Typography>
                          {summary && (
                            <Typography
                              variant="caption"
                              color="text.secondary"
                              sx={{ display: 'block', fontFamily: 'monospace', opacity: 0.75 }}
                            >
                              {c.measure?.label}: {summary}
                            </Typography>
                          )}
                        </Box>
                      </Box>
                    );
                  })}
                </Stack>
              </Box>
            ))}
          </ToggleSection>
        </Box>
      )}
    </Box>
  );
}
