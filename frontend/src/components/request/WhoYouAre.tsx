import { useEffect, useState } from 'react';
import { Autocomplete, Box, Chip, CircularProgress, Link, TextField, Typography } from '@mui/material';
import { Person, VerifiedEmail, errorText, getDepartments, searchSupervisors } from '../../api/intake';

/** What a new starter says about themselves before their first request (#226). */
export interface NewStarter {
  first: string;
  last: string;
  /** As the Staff Directory spells it. */
  department: string;
  /** Picked from the search, or null. */
  supervisor: Person | null;
  /** True when they could not find the supervisor and type the email instead. */
  useEmail: boolean;
  supervisorEmail: string;
}

export const EMPTY_STARTER: NewStarter = {
  first: '', last: '', department: '', supervisor: null, useEmail: false, supervisorEmail: '',
};

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const SEARCH_DELAY_MS = 250;     // wait for a pause in typing before asking the server
const MIN_LETTERS = 2;           // the server returns nobody before this

/**
 * Whether everything needed to move on to the request is filled in.
 *
 * @param s - The details so far.
 * @returns True when ready.
 */
export function starterReady(s: NewStarter): boolean {
  const supervisorOk = s.useEmail ? EMAIL_PATTERN.test(s.supervisorEmail.trim()) : !!s.supervisor;
  return !!s.first.trim() && !!s.last.trim() && !!s.department && supervisorOk;
}

/**
 * First and last name, department, then the supervisor: a typed search over
 * everyone in that department, or their work email when the search does not
 * find them.
 *
 * @param props.verified - The new starter's verified-email token; every call needs it.
 * @param props.value - The details so far.
 * @param props.onChange - Called with the updated details.
 * @param props.onError - Called with a readable reason when a list cannot load.
 * @returns The fields.
 */
export default function WhoYouAre({ verified, value, onChange, onError }: {
  verified: VerifiedEmail;
  value: NewStarter;
  onChange: (next: NewStarter) => void;
  onError: (message: string) => void;
}) {
  const [departments, setDepartments] = useState<string[] | null>(null);   // null while loading
  const [typed, setTyped] = useState('');
  const [found, setFound] = useState<Person[]>([]);
  const [searching, setSearching] = useState(false);

  /** Change some fields. @param part - The fields to change. */
  const set = (part: Partial<NewStarter>) => onChange({ ...value, ...part });

  // The departments, once.
  useEffect(() => {
    getDepartments(verified)
      .then((r) => setDepartments(r.data.departments || []))
      .catch((err) => {
        setDepartments([]);
        onError(errorText(err, 'The departments could not be loaded. Refresh the page and start again.'));
      });
  }, [verified]);                                    // onError left out: a new function each render

  // Search after a short pause in typing; a newer search cancels the older one.
  useEffect(() => {
    if (!value.department || typed.trim().length < MIN_LETTERS) {
      setFound([]);
      return;
    }
    let stale = false;
    const timer = setTimeout(async () => {
      setSearching(true);
      try {
        const r = await searchSupervisors(verified, value.department, typed.trim());
        if (!stale) setFound(r.data.people || []);
      } catch {
        if (!stale) setFound([]);                      // a failed search just shows nobody
      } finally {
        if (!stale) setSearching(false);
      }
    }, SEARCH_DELAY_MS);
    return () => { stale = true; clearTimeout(timer); };
  }, [typed, value.department, verified]);

  const emailBad = value.useEmail && !!value.supervisorEmail.trim() && !EMAIL_PATTERN.test(value.supervisorEmail.trim());

  return (
    <Box sx={{ display: 'grid', gap: 2.5 }}>
      <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'repeat(2, minmax(0, 1fr))' } }}>
        <TextField label="First name" required autoComplete="given-name"
          value={value.first} onChange={(e) => set({ first: e.target.value })} />
        <TextField label="Last name" required autoComplete="family-name"
          value={value.last} onChange={(e) => set({ last: e.target.value })} />
      </Box>

      <Box sx={{ display: 'grid', gap: 1 }}>
        <Typography sx={{ fontWeight: 600, fontSize: 14 }}>Your department</Typography>
        {departments === null ? <CircularProgress size={20} /> : (
          <Box role="radiogroup" aria-label="Your department" sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
            {departments.map((d) => {
              const on = value.department === d;
              return (
                <Chip
                  key={d} label={d} role="radio" aria-checked={on}
                  color={on ? 'primary' : 'default'} variant={on ? 'filled' : 'outlined'}
                  // A new department means a new search: the old pick may not be in it.
                  onClick={() => { if (!on) { setTyped(''); set({ department: d, supervisor: null }); } }}
                />
              );
            })}
          </Box>
        )}
      </Box>

      {value.department && (
        <Box sx={{ display: 'grid', gap: 1 }}>
          <Typography sx={{ fontWeight: 600, fontSize: 14 }}>Your supervisor</Typography>
          {!value.useEmail ? (
            <>
              <Autocomplete
                options={found}
                value={value.supervisor}
                onChange={(_, picked) => set({ supervisor: picked })}
                inputValue={typed}
                onInputChange={(_, text) => setTyped(text)}
                getOptionLabel={(p) => p.name}
                isOptionEqualToValue={(a, b) => a.id === b.id}
                filterOptions={(o) => o}                   // the server already filtered
                loading={searching}
                noOptionsText={typed.trim().length < MIN_LETTERS ? 'Type at least two letters of their name' : `Nobody in ${value.department} by that name`}
                renderInput={(params) => (
                  <TextField {...params} label={`Search ${value.department}`} placeholder="Start typing their name" />
                )}
              />
              <Link component="button" type="button" variant="body2" sx={{ justifySelf: 'start' }}
                onClick={() => set({ useEmail: true, supervisor: null })}>
                Can't find them? Use their work email
              </Link>
            </>
          ) : (
            <>
              <TextField label="Supervisor's work email" type="email" autoComplete="off"
                value={value.supervisorEmail} onChange={(e) => set({ supervisorEmail: e.target.value })}
                error={emailBad} helperText={emailBad ? 'Enter a full email address.' : 'They will be asked to add you.'} />
              <Link component="button" type="button" variant="body2" sx={{ justifySelf: 'start' }}
                onClick={() => set({ useEmail: false, supervisorEmail: '' })}>
                Search by name instead
              </Link>
            </>
          )}
        </Box>
      )}
    </Box>
  );
}
