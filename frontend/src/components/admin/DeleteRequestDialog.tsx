import { useEffect, useState } from 'react';
import {
  Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel,
  Radio, RadioGroup, TextField, Typography,
} from '@mui/material';
import { useTheme } from '@mui/material/styles';
import KindPill from '../KindPill';
import { Row, amountText, whenText } from './requestTable';

/** How an approved request's balance change is handled on delete. */
export type DeleteMode = 'refund' | 'keep';

/**
 * Whether an approved request's balance change is on record (#402): its
 * "Taken from" says recorded or counted. An approved request with nothing on
 * record (never applied, or settled as no balance needed) has nothing a refund
 * should give back, so it is never refunded on delete.
 *
 * @param r - A request row.
 * @returns True when deleting it could refund recorded days.
 */
export function movedBalance(r: Row): boolean {
  const state = r.raw?.taken_from?.state;                       // stamped by the admin endpoints
  return r.status === 'Approved' && (state === 'recorded' || state === 'counted');
}

interface Props {
  /** The rows to delete, or null when the dialog is closed. */
  rows: Row[] | null;
  onClose: () => void;
  /** Confirm: the admin's reason and whether to refund approved rows first. */
  onConfirm: (reason: string, refund: boolean) => void;
}

/**
 * The delete dialog (#282): the requests being deleted, a required reason, and,
 * when any are approved, a choice to refund their balance change first or
 * delete only. Confirming hands the reason and the refund choice back; the
 * screen animates the rows out and offers Undo before the server call lands.
 *
 * @param props - See {@link Props}.
 * @returns The dialog.
 */
export default function DeleteRequestDialog({ rows, onClose, onConfirm }: Props) {
  const theme = useTheme();
  const [reason, setReason] = useState('');
  const [mode, setMode] = useState<DeleteMode>('refund');      // only used when some rows are approved
  const open = rows !== null;

  // A fresh open starts with an empty reason and the default (refund) choice.
  useEffect(() => {
    if (open) { setReason(''); setMode('refund'); }
  }, [open]);

  const list = rows || [];
  const approved = list.filter(movedBalance);                   // only these changed balances
  const unmoved = list.filter((r) => r.status === 'Approved' && !movedBalance(r));   // approved, nothing on record
  const canDelete = reason.trim().length > 0;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{list.length === 1 ? `Delete request ${list[0]?.id}?` : `Delete ${list.length} requests?`}</DialogTitle>
      <DialogContent dividers sx={{ display: 'grid', gap: 2 }}>
        {/* The requests being deleted, up to six, then a count. */}
        <Box component="ul" sx={{ m: 0, pl: 0, listStyle: 'none', display: 'grid', gap: 0.75 }}>
          {list.slice(0, 6).map((r) => (
            <Box component="li" key={r.key} sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, alignItems: 'center', fontSize: 14 }}>
              <KindPill kind={r.kind} label={r.kindLabel} />
              <span>{r.person}{whenText(r) ? `, ${whenText(r)}` : ''}, {amountText(r)}</span>
              <Box component="span" sx={{ color: 'text.secondary' }}>· {r.status}</Box>
            </Box>
          ))}
          {list.length > 6 && <Box component="li" sx={{ color: 'text.secondary', fontSize: 13 }}>and {list.length - 6} more</Box>}
        </Box>

        {/* Approved rows already changed balances: refund first, or delete only. */}
        {approved.length > 0 && (
          <Box sx={{ bgcolor: theme.tokens.warnSoft, borderRadius: 2, p: 1.5, display: 'grid', gap: 1 }}>
            <Typography sx={{ fontWeight: 700, fontSize: 14 }}>
              {approved.length === 1
                ? '1 of these is approved and already changed balances.'
                : `${approved.length} of these are approved and already changed balances.`}
            </Typography>
            <RadioGroup value={mode} onChange={(e) => setMode(e.target.value as DeleteMode)}>
              <FormControlLabel
                value="refund" control={<Radio size="small" />}
                label={<Box><b>Refund, then delete</b><Typography variant="caption" color="text.secondary" display="block">Put the days back first, so balances are as if the request never happened.</Typography></Box>}
              />
              <FormControlLabel
                value="keep" control={<Radio size="small" />}
                label={<Box><b>Delete only</b><Typography variant="caption" color="text.secondary" display="block">Balances stay as they are now. Use this for duplicates whose days were never meant to come back.</Typography></Box>}
              />
            </RadioGroup>
          </Box>
        )}

        {/* Approved, but no balance change on record: deleting moves nothing (#402). */}
        {unmoved.length > 0 && (
          <Typography variant="body2" color="text.secondary">
            {unmoved.length === 1
              ? '1 of these is approved but has no balance change on record, so deleting it changes no balances.'
              : `${unmoved.length} of these are approved but have no balance change on record, so deleting them changes no balances.`}
          </Typography>
        )}

        <TextField
          label="Reason" required autoFocus fullWidth
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && canDelete) { e.preventDefault(); onConfirm(reason.trim(), mode === 'refund'); } }}
          placeholder="Required. e.g. Duplicate of 3905, submitted twice"
          helperText="Deleted requests are kept in the request log with this reason."
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="contained" color="error" disabled={!canDelete}
          onClick={() => onConfirm(reason.trim(), mode === 'refund')}
        >
          {list.length === 1 ? 'Delete' : `Delete ${list.length}`}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
