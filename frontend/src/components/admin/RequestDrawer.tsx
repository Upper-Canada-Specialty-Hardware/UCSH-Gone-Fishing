import { Box, Button, Chip, Drawer, IconButton, Stack, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import CloseIcon from '@mui/icons-material/Close';
import KindPill from '../KindPill';
import { DIAGNOSTIC_LABELS, OPEN_HELD, RequestRow } from './requestRows';

/** The balances shown in the panel, in the pending endpoint's keys. */
const BALANCES: { key: string; label: string }[] = [
  { key: 'vacation_balance', label: 'Vacation' },
  { key: 'sick_balance', label: 'Sick' },
  { key: 'overtime', label: 'Make-Up' },
  { key: 'carryover', label: 'Carry Over' },
];

/** The actions the panel can start; the screen owns what each does. */
export interface RequestActions {
  approve: (r: RequestRow) => void;
  reject: (r: RequestRow) => void;
  edit: (r: RequestRow) => void;
  remind: (r: RequestRow) => void;
  refund: (r: RequestRow) => void;
  reprocess: (r: RequestRow) => void;
  audit: (r: RequestRow) => void;
  addHeld: (r: RequestRow) => void;
  retryHeld: (r: RequestRow) => void;
  cancelHeld: (r: RequestRow) => void;
}

interface Props {
  /** The open request, or null when the panel is closed. */
  row: RequestRow | null;
  onClose: () => void;
  actions: RequestActions;
  /** Whether writes are enabled; write actions are disabled in reporting-only mode. */
  processingEnabled: boolean;
  /** True while an action on this row is running. */
  busy: boolean;
}

/**
 * The buttons a row offers, by where it came from and its status. Shared by
 * the table (the main action) and the panel (all of them).
 *
 * @param r - The row.
 * @returns Each action's label, handler key, style, and whether it writes.
 */
export function rowActions(r: RequestRow): { label: string; act: keyof RequestActions; tone: 'ok' | 'err' | 'out' | 'ghost'; writes: boolean }[] {
  if (r.source === 'pending') {
    return [
      { label: 'Approve', act: 'approve', tone: 'ok', writes: true },
      { label: 'Reject', act: 'reject', tone: 'err', writes: true },
      { label: 'Edit', act: 'edit', tone: 'out', writes: true },
      { label: 'Send reminder', act: 'remind', tone: 'ghost', writes: true },
    ];
  }
  if (r.source === 'stuck') return [{ label: 'Reprocess', act: 'reprocess', tone: 'out', writes: true }];
  if (r.source === 'held') {
    if (!OPEN_HELD.includes(r.raw.status)) return [];          // sent or cancelled: nothing to do
    return [
      // Only someone not on staff yet can be added; the others are already there.
      ...(r.raw.status === 'held' ? [{ label: 'Add employee', act: 'addHeld' as const, tone: 'out' as const, writes: false }] : []),
      { label: 'Retry', act: 'retryHeld', tone: 'ghost', writes: true },
      { label: 'Cancel', act: 'cancelHeld', tone: 'ghost', writes: false },
    ];
  }
  // History: an approved request can be refunded; approved or refunded ones have a trail.
  return [
    ...(r.status === 'Approved' ? [{ label: 'Refund', act: 'refund' as const, tone: 'out' as const, writes: true }] : []),
    ...(r.audit ? [{ label: 'Audit trail', act: 'audit' as const, tone: 'ghost' as const, writes: false }] : []),
  ];
}

/**
 * One action button in the panel's or table's style.
 *
 * @param props.tone - ok (approve), err (reject), out (outlined), ghost (text).
 * @returns The button.
 */
export function ActionButton({ tone, label, disabled, onClick }: { tone: 'ok' | 'err' | 'out' | 'ghost'; label: string; disabled: boolean; onClick: () => void }) {
  const variant = tone === 'ok' || tone === 'err' ? 'contained' : tone === 'out' ? 'outlined' : 'text';
  const color = tone === 'ok' ? 'success' : tone === 'err' ? 'error' : 'primary';
  return (
    <Button
      size="small"
      variant={variant}
      color={color}
      disabled={disabled}
      title={disabled ? 'Processing is currently disabled' : undefined}
      onClick={(e) => { e.stopPropagation(); onClick(); }}   // a click in a row must not also open the panel
    >
      {label}
    </Button>
  );
}

/**
 * The side panel for one request: what it is, who it is for, why it is stuck
 * or held, what approving it does to the balances, and every action it allows.
 *
 * @param props - See {@link Props}.
 * @returns The panel (a right-hand drawer).
 */
export default function RequestDrawer({ row, onClose, actions, processingEnabled, busy }: Props) {
  const theme = useTheme();
  const r = row;
  const cur = r?.raw.current_balances;                   // pending only
  const next = r?.raw.projected_balances;

  return (
    <Drawer anchor="right" open={!!r} onClose={onClose} PaperProps={{ sx: { width: { xs: '100%', sm: 420 } } }}>
      {r && (
        <Box sx={{ display: 'grid', alignContent: 'start' }}>
          <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'flex-start', p: 2, borderBottom: 1, borderColor: 'divider' }}>
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <Typography sx={{ fontWeight: 700, fontSize: 17 }}>{r.who}</Typography>
              <Typography variant="body2" color="text.secondary">
                {r.source === 'held' ? 'Held request' : `#${r.id}`} · {r.status}
                {r.waiting !== null && ` · waiting ${r.waiting} day${r.waiting === 1 ? '' : 's'}`}
              </Typography>
            </Box>
            <IconButton aria-label="Close" onClick={onClose} size="small"><CloseIcon /></IconButton>
          </Box>

          <Stack spacing={2} sx={{ p: 2 }}>
            {/* What it is, in a short labelled list. */}
            <Box component="dl" sx={{ m: 0, display: 'grid', gridTemplateColumns: 'auto 1fr', columnGap: 2, rowGap: 1, '& dt': { color: 'text.secondary', fontSize: 13 }, '& dd': { m: 0 } }}>
              <dt>Request</dt>
              <dd><KindPill kind={r.kind} label={r.what} /> {r.when && <Typography component="span" variant="body2">{r.when}</Typography>}</dd>
              {(r.raw.Days != null || r.raw.Hours != null) && (
                <>
                  <dt>Amount</dt>
                  <dd>{r.request_type === 'overtime' ? `${r.raw.Hours} hours` : `${r.raw.Days} days`}</dd>
                </>
              )}
              <dt>{r.source === 'held' ? 'Summary' : 'Description'}</dt>
              <dd>{r.note || '-'}</dd>
              <dt>{r.source === 'held' ? 'Supervisor' : 'Manager'}</dt>
              <dd>{r.manager || 'None'}</dd>
              {r.source === 'held' && (
                <>
                  <dt>Email</dt>
                  <dd>{r.raw.email} · {r.raw.location}</dd>
                </>
              )}
            </Box>

            {/* Stuck: why, in the backend's words, then each problem as a chip. */}
            {r.issues.length > 0 && (
              <Box>
                <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap">
                  {r.issues.map((code) => {
                    const d = DIAGNOSTIC_LABELS[code] || { label: code, color: 'warning' as const };
                    return <Chip key={code} size="small" color={d.color} label={d.label} />;
                  })}
                </Stack>
              </Box>
            )}
            {r.detail && <Typography variant="body2" color={r.source === 'held' ? 'error' : 'text.secondary'}>{r.detail}</Typography>}

            {/* Pending: each balance now and after approval; a drop is shown in the warn colour. */}
            {cur && (
              <Box>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>Balances if approved</Typography>
                {r.raw.balance_unchanged ? (
                  <Typography variant="body2" sx={{ fontStyle: 'italic' }}>{r.raw.balance_unchanged}</Typography>
                ) : (
                  <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 1 }}>
                    {BALANCES.map(({ key, label }) => {
                      const a = cur[key] ?? 0, b = next?.[key] ?? a;
                      return (
                        <Box key={key} sx={{ p: 1.25, borderRadius: '10px', bgcolor: theme.tokens.hover }}>
                          <Typography variant="caption" color="text.secondary">{label}</Typography>
                          <Typography sx={{ fontWeight: 600 }}>
                            {a} →{' '}
                            <Box component="span" sx={{ color: b < a ? 'warning.main' : b > a ? 'success.main' : 'inherit' }}>{b}</Box>
                          </Typography>
                        </Box>
                      );
                    })}
                  </Box>
                )}
              </Box>
            )}

            <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
              {rowActions(r).map((a) => (
                <ActionButton
                  key={a.act}
                  tone={a.tone}
                  label={a.label}
                  disabled={busy || (a.writes && !processingEnabled)}
                  onClick={() => actions[a.act](r)}
                />
              ))}
            </Stack>
          </Stack>
        </Box>
      )}
    </Drawer>
  );
}
