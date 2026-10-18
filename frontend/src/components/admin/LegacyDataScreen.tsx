import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Box, Button, Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle,
  Paper, Snackbar, Stack, TextField, Typography,
} from '@mui/material';
import { useNavigate } from 'react-router-dom';
import {
  FindingStatus, FindingsPage, LegacyFinding, getLegacyFindings, ignoreLegacyFinding, resolveLegacyFinding,
} from '../../api/legacy';
import { requestTypeOf } from '../../api/events';

/** A plain-words title and one sentence on what each reconciliation case means. */
interface CaseInfo {
  title: string;
  blurb: string;
}

/**
 * Every finding case in plain words (#337). The key matches the backend's
 * request_vocab.FINDING_CASES; a case the backend adds later falls back to its
 * raw key, so the screen never hides a finding it does not recognise.
 */
const CASE_INFO: Record<string, CaseInfo> = {
  approved_no_balance_change: { title: 'Approved, no balance change', blurb: 'A request was approved but no balance ever moved for it.' },
  employment_type_suspect: { title: 'Employment type looks wrong', blurb: 'The recorded salary or hourly type does not match how the request was handled.' },
  hourly_vacation_leftover: { title: 'Hourly vacation figure left over', blurb: 'An hourly person has a vacation figure from before the hourly rules. It is kept as it is; HR decides what it should be.' },
  date_error: { title: 'Date errors', blurb: "A request's start or end date is missing, or the end comes before the start." },
  stuck_cancelled: { title: 'Stuck cancelled', blurb: 'A request marked cancelled that never finished being cancelled.' },
  refunded_marked_processed: { title: 'Refunded but marked processed', blurb: 'A refunded request still shows as processed.' },
  submitter_unmatched: { title: 'Submitter not matched', blurb: 'The person who submitted the request could not be matched to a staff record.' },
  username_as_name: { title: 'Username used as a name', blurb: "A record stores a sign-in name where a person's name belongs." },
  no_user_link: { title: 'No user link', blurb: 'A record has no link to a directory user.' },
  below_zero_predates_rules: { title: 'Below zero before the hourly rules', blurb: 'A balance went below zero before the hourly rules took effect.' },
  sharepoint_new_after_switch: { title: 'New in SharePoint after the switch', blurb: 'A new SharePoint item appeared after the move to the new system.' },
  sharepoint_edit_after_switch: { title: 'Edited in SharePoint after the switch', blurb: 'A SharePoint item was changed after the move to the new system.' },
  sharepoint_delete_after_switch: { title: 'Deleted in SharePoint after the switch', blurb: 'A SharePoint item was deleted after the move to the new system.' },
};

/** The order cases are shown in; the CASE_INFO keys, so related cases sit together. */
const CASE_ORDER = Object.keys(CASE_INFO);

/** The three status views, left to right. */
const STATUS_TABS: { key: FindingStatus; label: string }[] = [
  { key: 'open', label: 'Open' },
  { key: 'resolved', label: 'Resolved' },
  { key: 'ignored', label: 'Ignored' },
];

/** How many findings to load at once; one case at a time stays well under this. */
const PAGE_SIZE = 200;

/**
 * Turn a snake_case evidence key into a readable label, e.g. ``start_date`` to
 * "Start date".
 *
 * @param key - The evidence key.
 * @returns The humanised label.
 */
function humanizeKey(key: string): string {
  const spaced = key.replace(/_/g, ' ').trim();                // start_date -> start date
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);     // -> Start date
}

/**
 * Render one evidence value as readable text, never a raw JSON blob.
 *
 * @param value - The value from the evidence object.
 * @returns A short string; an object or array is compacted to one line.
 */
function renderValue(value: unknown): string {
  if (value === null || value === undefined) return '-';       // show a dash, not "null"
  if (typeof value === 'object') return JSON.stringify(value); // nested: one compact line
  return String(value);                                        // primitives as-is
}

/**
 * How a finding names its subject, so HR knows which record it is about.
 *
 * @param finding - The finding.
 * @returns "request #123 (leave)" or "staff record 45".
 */
function subjectLabel(finding: LegacyFinding): string {
  if (finding.subject === 'staff') return `staff record ${finding.subject_id}`;
  const kind = finding.kind ? ` (${finding.kind.replace(/_/g, ' ')})` : '';   // carryover_payout -> carryover payout
  return `request #${finding.subject_id}${kind}`;
}

/** A finding and the decision being taken on it, while the note dialog is open. */
interface Pending {
  finding: LegacyFinding;
  action: 'resolve' | 'ignore';
}

interface Props {
  /** Surface a message on the dashboard snackbar. */
  onNotify?: (message: string, severity: 'success' | 'error') => void;
}

/**
 * The admin legacy data handler (#337): the reconciliation findings the Postgres
 * move turns up, grouped by case with a plain-words title and one sentence each,
 * filtered by open, resolved or ignored, with a per-case count. Each finding
 * shows its reason and its evidence in readable form and a link to the person or
 * the request; resolving or ignoring one needs a note. Everything is cards and
 * stacks, so nothing scrolls sideways on a phone. Nothing here changes a balance
 * or a request: HR fixes the underlying record through the existing hand edit.
 *
 * @param props - See {@link Props}.
 * @returns The legacy data screen.
 */
export default function LegacyDataScreen({ onNotify }: Props) {
  const navigate = useNavigate();
  const [status, setStatus] = useState<FindingStatus>('open');
  const [caseFilter, setCaseFilter] = useState<string | null>(null);
  const [page, setPage] = useState(1);                      // 1-based; reset to 1 on a status/case change
  const [data, setData] = useState<FindingsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [pending, setPending] = useState<Pending | null>(null);
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [snack, setSnack] = useState<{ open: boolean; message: string }>({ open: false, message: '' });
  // Each load takes a ticket; only the newest ticket's answer is applied, so a
  // quick chip switch cannot paint the previous filter's results over the new.
  const reqSeq = useRef(0);

  /**
   * Fetch one page of findings for the current status and case. Page 1 replaces
   * the list; a later page appends, for the "Load more" control.
   *
   * @param targetPage - The 1-based page to fetch.
   */
  const load = useCallback((targetPage: number) => {
    const seq = ++reqSeq.current;                           // this request's ticket
    setLoading(true);
    setError('');
    getLegacyFindings({ status, case: caseFilter || undefined, page: targetPage, page_size: PAGE_SIZE })
      .then((res) => {
        if (seq !== reqSeq.current) return;                 // a newer request started: drop this answer
        setData((prev) => (targetPage <= 1 || !prev
          ? res.data                                        // first page: replace
          : { ...res.data, findings: [...prev.findings, ...res.data.findings] }));   // later page: append
      })
      .catch(() => { if (seq === reqSeq.current) setError('The findings could not be loaded.'); })
      .finally(() => { if (seq === reqSeq.current) setLoading(false); });
  }, [status, caseFilter]);
  useEffect(() => { load(page); }, [load, page]);

  /** Load the next page of findings (keeps what is already shown). */
  const loadMore = useCallback(() => setPage((p) => p + 1), []);

  // The counts come back for the whole status, so the overview chips are always
  // complete even when only one case's findings are loaded.
  const counts = data?.counts || {};
  // Cases with at least one finding at this status, in the fixed order.
  const casesWithFindings = useMemo(() => CASE_ORDER.filter((c) => (counts[c] || 0) > 0), [counts]);

  // The loaded findings grouped by case, in the fixed case order.
  const grouped = useMemo(() => {
    const byCase = new Map<string, LegacyFinding[]>();
    (data?.findings || []).forEach((f) => {
      const list = byCase.get(f.case) || [];
      list.push(f);
      byCase.set(f.case, list);
    });
    return CASE_ORDER
      .filter((c) => byCase.has(c))
      .map((c) => [c, byCase.get(c)!] as [string, LegacyFinding[]]);
  }, [data]);

  const total = data?.total ?? 0;
  // Count only findings that still match the current status, so a card decided
  // in place (it no longer matches) does not inflate the shown count.
  const loaded = data?.findings.filter((f) => f.status === status).length ?? 0;

  /** Open the note dialog for a resolve or ignore. */
  const startDecision = (finding: LegacyFinding, action: 'resolve' | 'ignore') => {
    setNote('');
    setPending({ finding, action });
  };

  /** Send the resolve or ignore with its note, then reload. */
  const confirmDecision = async () => {
    if (!pending || !note.trim()) return;
    setSaving(true);
    try {
      const call = pending.action === 'resolve' ? resolveLegacyFinding : ignoreLegacyFinding;
      const res = await call(pending.finding.id, note.trim());   // the finding with its new status and note
      const word = pending.action === 'resolve' ? 'resolved' : 'ignored';
      const caseKey = pending.finding.case;
      // Update the one card in place and drop it from this status's counts; no
      // full reload, so the scroll position is kept.
      setData((prev) => {
        if (!prev) return prev;
        const findings = prev.findings.map((f) => (f.id === res.data.id ? res.data : f));
        const counts = { ...prev.counts };
        if (counts[caseKey]) counts[caseKey] -= 1;
        return { ...prev, findings, counts, total: Math.max(0, prev.total - 1) };
      });
      setPending(null);
      onNotify?.(`Finding ${word}.`, 'success');
    } catch {
      setSnack({ open: true, message: 'That could not be saved. Try again shortly.' });
    } finally {
      setSaving(false);
    }
  };

  /**
   * Open the one record a finding is about, not the whole list. A staff finding
   * opens that person's profile by id; a request finding opens just that request
   * on the All requests screen. The screens read the `open` query param: a staff
   * id, or the request's row key history-<request type>-<id> (requestTypeOf maps
   * carryover_payout to the hyphenated url form the table uses).
   *
   * @param finding - The finding whose subject to open.
   */
  const openSubject = (finding: LegacyFinding) => {
    if (finding.subject === 'staff') {
      navigate(`/admin/staff?open=${encodeURIComponent(finding.subject_id)}`);   // the person's profile
      return;
    }
    const type = requestTypeOf(finding.kind || 'leave');                          // url-form request type
    navigate(`/admin/requests?open=${encodeURIComponent(`history-${type}-${finding.subject_id}`)}`);
  };

  return (
    <>
      <Typography color="text.secondary" sx={{ mt: -1.5 }}>
        Records the move to the new system flagged for a look. Nothing here is changed for you: open the
        person or the request, fix it with the usual edit, then mark the finding resolved or ignored with a note.
      </Typography>

      {/* Status view: open, resolved or ignored. */}
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
        {STATUS_TABS.map((t) => (
          <Chip
            key={t.key}
            label={t.label}
            color={status === t.key ? 'primary' : 'default'}
            variant={status === t.key ? 'filled' : 'outlined'}
            onClick={() => { setStatus(t.key); setCaseFilter(null); setPage(1); }}
            aria-pressed={status === t.key}
          />
        ))}
      </Stack>

      {/* Per-case overview: a chip per case with a finding, its count, click to narrow to it. */}
      {casesWithFindings.length > 0 && (
        <Paper sx={{ p: 2 }}>
          <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary', textTransform: 'uppercase', letterSpacing: '.06em' }}>
            Cases
          </Typography>
          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mt: 1 }}>
            <Chip
              label={`All (${total})`}
              color={caseFilter === null ? 'primary' : 'default'}
              variant={caseFilter === null ? 'filled' : 'outlined'}
              onClick={() => { setCaseFilter(null); setPage(1); }}
              aria-pressed={caseFilter === null}
            />
            {casesWithFindings.map((c) => (
              <Chip
                key={c}
                label={`${CASE_INFO[c]?.title || c} (${counts[c] || 0})`}
                color={caseFilter === c ? 'primary' : 'default'}
                variant={caseFilter === c ? 'filled' : 'outlined'}
                onClick={() => { setCaseFilter(c); setPage(1); }}
                aria-pressed={caseFilter === c}
              />
            ))}
          </Stack>
        </Paper>
      )}

      {loading && page === 1 ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', p: 6 }}><CircularProgress /></Box>
      ) : error ? (
        <Paper sx={{ p: 3 }}><Typography color="error">{error}</Typography></Paper>
      ) : grouped.length === 0 ? (
        <Paper sx={{ p: 4 }}>
          <Typography color="text.secondary">
            {status === 'open' ? 'No open findings. Nothing needs a look right now.' : `No ${status} findings.`}
          </Typography>
        </Paper>
      ) : (
        <Stack spacing={2.5}>
          {/* Always show how many of the total are loaded, with a Load more. */}
          {total > 0 && (
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
              <Typography variant="body2" color="text.secondary">
                Showing {loaded} of {total}{caseFilter === null ? '' : ' in this case'}.
              </Typography>
              {loaded < total && (
                <Button size="small" variant="outlined" onClick={loadMore} disabled={loading}>
                  {loading ? 'Loading...' : 'Load more'}
                </Button>
              )}
            </Box>
          )}
          {grouped.map(([caseKey, findings]) => {
            const info = CASE_INFO[caseKey];
            return (
              <Box key={caseKey}>
                {/* Case heading: plain-words title, its count, and one sentence on what it means. */}
                <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, flexWrap: 'wrap' }}>
                  <Typography variant="h6">{info?.title || caseKey}</Typography>
                  <Chip size="small" label={counts[caseKey] ?? findings.length} />
                </Box>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                  {info?.blurb || 'A record flagged for review.'}
                </Typography>
                <Stack spacing={1.5}>
                  {findings.map((f) => (
                    <FindingCard
                      key={f.id}
                      finding={f}
                      onOpen={() => openSubject(f)}
                      onResolve={() => startDecision(f, 'resolve')}
                      onIgnore={() => startDecision(f, 'ignore')}
                    />
                  ))}
                </Stack>
              </Box>
            );
          })}
        </Stack>
      )}

      {/* Resolve or ignore, both needing a note. */}
      <Dialog open={pending !== null} onClose={() => (saving ? null : setPending(null))} maxWidth="sm" fullWidth>
        <DialogTitle>{pending?.action === 'resolve' ? 'Resolve this finding' : 'Ignore this finding'}</DialogTitle>
        <DialogContent dividers>
          {pending && (
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              {CASE_INFO[pending.finding.case]?.title || pending.finding.case} · {subjectLabel(pending.finding)}
            </Typography>
          )}
          <TextField
            label="Note"
            placeholder={pending?.action === 'resolve' ? 'e.g. zeroed the balance in the person panel' : 'e.g. expected for this legacy record'}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            multiline
            rows={3}
            fullWidth
            required
            autoFocus
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPending(null)} disabled={saving}>Cancel</Button>
          <Button variant="contained" onClick={confirmDecision} disabled={saving || !note.trim()}>
            {pending?.action === 'resolve' ? 'Resolve' : 'Ignore'}
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={snack.open}
        autoHideDuration={4000}
        onClose={() => setSnack((s) => ({ ...s, open: false }))}
        message={snack.message}
      />
    </>
  );
}

/**
 * One finding as a card: its reason, its evidence in readable rows, the subject
 * and an Open link, plus Resolve and Ignore for an open one or the note and who
 * decided for a closed one.
 *
 * @param props.finding - The finding.
 * @param props.onOpen - Go to the person or the request to fix it.
 * @param props.onResolve - Start resolving it (open the note dialog).
 * @param props.onIgnore - Start ignoring it (open the note dialog).
 * @returns The card.
 */
function FindingCard({ finding, onOpen, onResolve, onIgnore }: {
  finding: LegacyFinding; onOpen: () => void; onResolve: () => void; onIgnore: () => void;
}) {
  const evidenceEntries = Object.entries(finding.evidence || {});
  const decided = finding.status !== 'open';
  return (
    <Paper variant="outlined" sx={{ p: 2, display: 'grid', gap: 1.25 }}>
      <Typography sx={{ fontWeight: 600 }}>{finding.reason}</Typography>

      {/* Evidence as readable label and value rows, never a raw JSON blob. */}
      {evidenceEntries.length > 0 && (
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: 'minmax(0, 200px) minmax(0, 1fr)' }, gap: '4px 12px' }}>
          {evidenceEntries.map(([key, value]) => (
            <Box key={key} sx={{ display: 'contents' }}>
              <Typography variant="body2" color="text.secondary" sx={{ overflowWrap: 'anywhere' }}>{humanizeKey(key)}</Typography>
              <Typography variant="body2" sx={{ overflowWrap: 'anywhere', fontVariantNumeric: 'tabular-nums' }}>{renderValue(value)}</Typography>
            </Box>
          ))}
        </Box>
      )}

      {/* The subject and the link that opens where HR fixes it. */}
      <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
        <Typography variant="body2" color="text.secondary">{subjectLabel(finding)}</Typography>
        <Button size="small" variant="text" onClick={onOpen}>
          {finding.subject === 'staff' ? 'Open in Staff' : 'Open in Requests'}
        </Button>
      </Box>

      {decided ? (
        // Resolved or ignored: the note, who decided and when.
        <Box sx={{ pt: 0.5, borderTop: 1, borderColor: 'divider' }}>
          <Typography variant="body2">
            <Box component="span" color="text.secondary">{finding.status === 'resolved' ? 'Resolved' : 'Ignored'} by </Box>
            {finding.resolved_by || 'admin'}
            {finding.resolved_at ? <Box component="span" color="text.secondary"> on {new Date(finding.resolved_at).toLocaleDateString()}</Box> : null}
          </Typography>
          {finding.note && <Typography variant="body2" sx={{ mt: 0.5 }}>{finding.note}</Typography>}
        </Box>
      ) : (
        // Open: the two decisions, each needing a note.
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
          <Button size="small" variant="contained" onClick={onResolve}>Resolve</Button>
          <Button size="small" variant="outlined" color="inherit" onClick={onIgnore}>Ignore</Button>
        </Box>
      )}
    </Paper>
  );
}
