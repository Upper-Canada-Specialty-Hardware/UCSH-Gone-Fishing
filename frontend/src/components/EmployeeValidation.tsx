import { useMemo, useState } from 'react';
import {
  Box, Autocomplete, TextField, Button, CircularProgress, Alert,
  Chip, Stack, Typography, Paper, Divider,
} from '@mui/material';
import { validateEmployee } from '../api/client';

// One check row and the whole report, matching the backend
// employee_validation.build_validation_report shape.
interface Check {
  code: string;
  category: string;
  status: 'pass' | 'warn' | 'fail';
  detail: string;
  projected: Record<string, number> | null;
}
interface Report {
  employee_id: string;
  employee_name: string;
  overall: 'pass' | 'warn' | 'fail';
  checks: Check[];
}

const STATUS: Record<Check['status'], { color: 'success' | 'warning' | 'error'; label: string }> = {
  pass: { color: 'success', label: 'PASS' },
  warn: { color: 'warning', label: 'WARN' },
  fail: { color: 'error', label: 'FAIL' },
};

const OVERALL_SEVERITY: Record<Report['overall'], 'success' | 'warning' | 'error'> = {
  pass: 'success',
  warn: 'warning',
  fail: 'error',
};
const OVERALL_TEXT: Record<Report['overall'], string> = {
  pass: 'all checks passed — this employee is set up correctly.',
  warn: 'works, but with warnings worth reviewing.',
  fail: 'setup problems found — a real request would break or get stuck.',
};

// Fixed display order; only categories present in the report are rendered.
const CATEGORY_ORDER = ['identity', 'supervisor', 'location', 'balances', 'simulation'];
const CATEGORY_LABELS: Record<string, string> = {
  identity: 'Identity',
  supervisor: 'Supervisor',
  location: 'Location & Holidays',
  balances: 'Balances',
  simulation: 'Simulations (per leave type / workflow)',
};

// SP balance-pot column names -> friendly labels for the projected line.
const POT_LABELS: Record<string, string> = {
  CurrentVacationBalance: 'Vacation',
  CurrentSickDayBalance: 'Sick',
  CurrentOvertimeBalance: 'Make-Up',
  CarryOver: 'Carry Over',
  Payout: 'Payout',
};

interface Props {
  employees: any[];
}

export default function EmployeeValidation({ employees }: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState('');

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
      setError(typeof detail === 'string' ? detail : 'Validation failed');
    } finally {
      setLoading(false);
    }
  };

  const grouped = useMemo(() => {
    const g: Record<string, Check[]> = {};
    (report?.checks || []).forEach((c) => {
      if (!g[c.category]) g[c.category] = [];
      g[c.category].push(c);
    });
    return g;
  }, [report]);

  // Known categories first, then any category the backend added that this UI
  // doesn't yet know about — so a future check can never silently vanish.
  const orderedCategories = useMemo(() => [
    ...CATEGORY_ORDER.filter((cat) => grouped[cat]?.length),
    ...Object.keys(grouped).filter((cat) => !CATEGORY_ORDER.includes(cat)),
  ], [grouped]);

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Runs the employee's <strong>current Staff Directory values</strong> through every request
        workflow and leave type. Read-only — no request is created and no one is notified, so it is
        safe to run even in reporting-only mode.
      </Typography>

      <Box sx={{ display: 'flex', gap: 2, mb: 3, alignItems: 'center' }}>
        <Autocomplete
          options={employees}
          getOptionLabel={(opt: any) => `${opt.name} — ${opt.department}`}
          disabled={loading}
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
          Run Validation
        </Button>
      </Box>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {report && (
        <Box>
          <Alert severity={OVERALL_SEVERITY[report.overall] ?? 'warning'} sx={{ mb: 2 }}>
            <strong>{report.employee_name || `#${report.employee_id}`}</strong> — {OVERALL_TEXT[report.overall] ?? 'validation complete.'}
          </Alert>

          {orderedCategories.map((cat) => (
            <Paper key={cat} variant="outlined" sx={{ p: 2, mb: 2 }}>
              <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 1 }}>
                {CATEGORY_LABELS[cat] || cat}
              </Typography>
              <Stack divider={<Divider flexItem />} spacing={1}>
                {grouped[cat].map((c) => {
                  const st = STATUS[c.status] || STATUS.warn;
                  return (
                  <Box key={c.code} sx={{ display: 'flex', gap: 1.5, alignItems: 'flex-start' }}>
                    <Chip
                      size="small"
                      color={st.color}
                      label={st.label}
                      sx={{ minWidth: 64, fontWeight: 600 }}
                    />
                    <Box>
                      <Typography variant="body2">{c.detail}</Typography>
                      {c.projected && (
                        <Typography variant="caption" color="text.secondary">
                          Projected balances:{' '}
                          {Object.entries(c.projected)
                            .map(([k, v]) => `${POT_LABELS[k] || k} ${v}`)
                            .join(' · ')}
                        </Typography>
                      )}
                    </Box>
                  </Box>
                  );
                })}
              </Stack>
            </Paper>
          ))}
        </Box>
      )}
    </Box>
  );
}
