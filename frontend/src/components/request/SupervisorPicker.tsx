import { useEffect, useState } from 'react';
import { Autocomplete, Box, CircularProgress, Link, TextField, Typography } from '@mui/material';
import { Person, VerifiedEmail, searchSupervisors } from '../../api/intake';

/** The supervisor part of a form: a person picked from the search, or a typed work email. */
export interface SupervisorChoice {
  /** Picked from the department search, or null. */
  supervisor: Person | null;
  /** True when the search did not find them and the person types the email instead. */
  useEmail: boolean;
  /** The typed work email, when useEmail is true. */
  email: string;
}

/** A blank supervisor choice. */
export const EMPTY_SUPERVISOR: SupervisorChoice = { supervisor: null, useEmail: false, email: '' };

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;   // a full address, for the typed fallback
const SEARCH_DELAY_MS = 250;                          // wait for a pause in typing before asking
const MIN_LETTERS = 2;                                // the server returns nobody before this

/**
 * Whether a supervisor has been chosen (a picked person, or a valid typed email).
 *
 * @param c - The current choice.
 * @returns True when the choice is usable.
 */
export function supervisorReady(c: SupervisorChoice): boolean {
  return c.useEmail ? EMAIL_PATTERN.test(c.email.trim()) : !!c.supervisor;
}

/**
 * The supervisor fields a mover (and the re-pick) share: a typed search over
 * everyone in one department, with a fallback to the supervisor's work email.
 *
 * This is the same pattern WhoYouAre uses for a new hire, pulled out so the mover
 * suggested-changes step and the "not on my team" re-pick reuse one control (#442,
 * #443). The department is fixed by the caller here, so there is no department
 * chooser; a new department resets the search from outside via the key.
 *
 * @param props.verified - The person's verified-email token; every search needs it.
 * @param props.department - The department to search within, as the directory spells it.
 * @param props.value - The current choice.
 * @param props.onChange - Called with the updated choice.
 * @param props.label - Optional heading above the field; defaults to "Your supervisor".
 * @returns The supervisor field.
 */
export default function SupervisorPicker({ verified, department, value, onChange, label = 'Your supervisor' }: {
  verified: VerifiedEmail;
  department: string;
  value: SupervisorChoice;
  onChange: (next: SupervisorChoice) => void;
  label?: string;
}) {
  const [typed, setTyped] = useState('');            // what is in the search box right now
  const [found, setFound] = useState<Person[]>([]);  // the latest matches from the server
  const [searching, setSearching] = useState(false); // a request is in flight (shows the spinner)

  /** Change part of the choice. @param part - The fields to change. */
  const set = (part: Partial<SupervisorChoice>) => onChange({ ...value, ...part });

  // Search after a short pause in typing; a newer search cancels the older one.
  useEffect(() => {
    if (!department || typed.trim().length < MIN_LETTERS) {   // too little typed: show nobody
      setFound([]);
      return;
    }
    let stale = false;                               // set when a newer run supersedes this one
    const timer = setTimeout(async () => {
      setSearching(true);                            // the box shows it is working
      try {
        const r = await searchSupervisors(verified, department, typed.trim());
        if (!stale) setFound(r.data.people || []);   // only the newest run's results land
      } catch {
        if (!stale) setFound([]);                    // a failed search just shows nobody
      } finally {
        if (!stale) setSearching(false);
      }
    }, SEARCH_DELAY_MS);
    return () => { stale = true; clearTimeout(timer); };   // typing again cancels the pending search
  }, [typed, department, verified]);

  // A half-typed address is flagged, so the fallback does not silently fail.
  const emailBad = value.useEmail && !!value.email.trim() && !EMAIL_PATTERN.test(value.email.trim());

  return (
    <Box sx={{ display: 'grid', gap: 1 }}>
      <Typography sx={{ fontWeight: 600, fontSize: 14 }}>{label}</Typography>
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
            filterOptions={(o) => o}                 // the server already filtered by name
            loading={searching}
            noOptionsText={typed.trim().length < MIN_LETTERS
              ? 'Type at least two letters of their name'
              : `Nobody in ${department} by that name`}
            renderInput={(params) => (
              <TextField {...params} label={`Search ${department}`} placeholder="Start typing their name" />
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
            value={value.email} onChange={(e) => set({ email: e.target.value })}
            error={emailBad} helperText={emailBad ? 'Enter a full email address.' : 'They will be asked to confirm.'} />
          <Link component="button" type="button" variant="body2" sx={{ justifySelf: 'start' }}
            onClick={() => set({ useEmail: false, email: '' })}>
            Search by name instead
          </Link>
        </>
      )}
    </Box>
  );
}
