import { useMemo, useState } from 'react';
import { Box, Button, Checkbox, FormControlLabel, Stack, TextField, Typography } from '@mui/material';

/*
 * The people list inside an export dialog (#489): every person the export would
 * include, each with a tick, all ticked to start. Unticking someone leaves them
 * out of the file. Shared by the staff and requests exports so both read alike.
 */

/** One person the picker lists. */
export interface PickPerson {
  /** A stable key: the staff id, or the name when there is none. */
  key: string;
  name: string;
  /** A short muted line under the name, e.g. "Door · Toronto Victoria Park". */
  detail?: string;
}

/** Above this many people a search box is shown. */
const SEARCH_FROM = 8;

interface Props {
  people: PickPerson[];
  /** Keys of the people left OUT; everyone else is ticked. */
  excluded: Set<string>;
  onChange: (excluded: Set<string>) => void;
}

/**
 * A ticked list of people with search and all/none, for an export dialog.
 *
 * Holding the unticked keys (not the ticked ones) means a person who appears
 * later, when the shown list changes, starts ticked like everyone else.
 *
 * @param props - See {@link Props}.
 * @returns The list.
 */
export default function ExportPeoplePicker({ people, excluded, onChange }: Props) {
  const [query, setQuery] = useState('');
  const q = query.trim().toLowerCase();
  // The people the search matches; everyone when the search is empty.
  const visible = useMemo(
    () => (q ? people.filter((p) => `${p.name} ${p.detail || ''}`.toLowerCase().includes(q)) : people),
    [people, q],
  );
  const ticked = people.filter((p) => !excluded.has(p.key)).length;   // how many go in the file

  /** Tick or untick one person. */
  const toggle = (key: string) => {
    const next = new Set(excluded);
    if (next.has(key)) next.delete(key); else next.add(key);
    onChange(next);
  };

  /** Tick (all) or untick (none) everyone the search shows; the rest keep their state. */
  const setVisible = (on: boolean) => {
    const next = new Set(excluded);
    visible.forEach((p) => (on ? next.delete(p.key) : next.add(p.key)));
    onChange(next);
  };

  return (
    <Box sx={{ display: 'grid', gap: 0.75 }}>
      <Stack direction="row" alignItems="center" spacing={1}>
        <Typography variant="caption" color="text.secondary" sx={{ flex: 1 }}>
          {ticked} of {people.length} ticked
        </Typography>
        <Button size="small" onClick={() => setVisible(true)}>All</Button>
        <Button size="small" onClick={() => setVisible(false)}>None</Button>
      </Stack>
      {people.length > SEARCH_FROM && (
        <TextField size="small" placeholder="Find a person" value={query} onChange={(e) => setQuery(e.target.value)} inputProps={{ 'aria-label': 'Find a person' }} />
      )}
      {/* Its own scroll so a long list never pushes the column options away. */}
      <Box sx={{ maxHeight: 220, overflowY: 'auto', border: 1, borderColor: 'divider', borderRadius: 1.5, px: 1, py: 0.5, display: 'grid' }}>
        {visible.map((p) => (
          <FormControlLabel
            key={p.key}
            sx={{ alignItems: 'flex-start', m: 0, py: 0.25 }}
            control={<Checkbox size="small" sx={{ p: 0.5 }} checked={!excluded.has(p.key)} onChange={() => toggle(p.key)} />}
            label={(
              <Box sx={{ pt: 0.4 }}>
                <Typography variant="body2" sx={{ lineHeight: 1.3 }}>{p.name}</Typography>
                {p.detail && <Typography variant="caption" color="text.secondary">{p.detail}</Typography>}
              </Box>
            )}
          />
        ))}
        {visible.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ py: 1 }}>Nobody matches.</Typography>
        )}
      </Box>
    </Box>
  );
}
