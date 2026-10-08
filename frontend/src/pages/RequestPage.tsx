import { ReactNode, useEffect, useMemo, useState } from 'react';
import {
  Box, Typography, TextField, Button, Alert, CircularProgress, MenuItem, Link,
} from '@mui/material';
import { ThemeProvider, useTheme } from '@mui/material/styles';
import RequestForm, {
  EMPTY_REQUEST, RequestValues, buildRequestBody, isRequestReady,
} from '../components/RequestForm';
import CodeBoxes from '../components/request/CodeBoxes';
import { NextItem, NextSteps, SidePanel, StepItem, TypeCards } from '../components/request/RequestLayout';
import { DEFAULT_WORDS, MOODS, moodOf } from '../components/request/moods';
import { buildTheme } from '../theme';
import {
  EmployeeSession, RequestType, Supervisor, VerifiedEmail,
  clearSession, dashboardHref, errorText, getSupervisors, holdRequest,
  isHandedOff, loadSession, requestCode, saveSession, submitMyRequest, verifyCode,
} from '../api/intake';

/**
 * Where the person is in the flow.
 * - email: type your email
 * - code: type the 6-digit code that was emailed
 * - form: signed in as an employee; pick a form and fill it in
 * - newhire: email is on no staff record; say who you are, pick a supervisor, fill in the form
 * - done: submitted (or held for a new hire)
 */
type Step = 'email' | 'code' | 'form' | 'newhire' | 'done';

const RESEND_SECONDS = 60;   // wait before "send a new code" is offered

/** Each step's name in the side panel. */
const STEP_LABELS: Record<Step, string> = {
  email: 'Your email',
  code: 'The code we email you',
  form: 'Your request',
  newhire: 'About you and your request',
  done: 'Sent',
};

/**
 * A step's heading, in the display serif.
 *
 * @param props.children - The text.
 * @returns The heading.
 */
function Heading({ children }: { children: ReactNode }) {
  const theme = useTheme();
  return (
    <Typography component="h2" sx={{ fontFamily: theme.tokens.display, fontSize: 26, fontWeight: 600, letterSpacing: '-0.01em' }}>
      {children}
    </Typography>
  );
}

/**
 * The short line under a heading.
 *
 * @param props.children - The text.
 * @returns The line, pulled up under the heading.
 */
function Lead({ children }: { children: ReactNode }) {
  return <Typography color="text.secondary" sx={{ mt: -1.25 }}>{children}</Typography>;
}

/**
 * Who is signed in, with a way out.
 *
 * @param props.name - The employee.
 * @param props.fromDashboard - True when the sign-in came from their dashboard.
 * @param props.onSignOut - Forget the sign-in.
 * @returns The strip.
 */
function SignedInAs({ name, fromDashboard, onSignOut }: { name: string; fromDashboard: boolean; onSignOut: () => void }) {
  const theme = useTheme();
  const initials = name.split(' ').map((w) => w[0]).slice(0, 2).join('');   // e.g. "AE"
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, bgcolor: theme.tokens.hover, borderRadius: '12px', px: 1.5, py: 1.25, flexWrap: 'wrap' }}>
      <Box sx={{
        width: 30, height: 30, borderRadius: '50%', bgcolor: 'primary.main', color: theme.tokens.primaryInk,
        display: 'grid', placeItems: 'center', fontSize: 12, fontWeight: 700, flex: 'none', transition: 'background-color .5s ease',
      }}>
        {initials}
      </Box>
      <Typography variant="body2" sx={{ flex: 1, minWidth: 160 }}>
        Signed in as <b>{name}</b>{fromDashboard ? ' from your dashboard' : ''}
      </Typography>
      <Link component="button" variant="body2" onClick={onSignOut}>Not you? Sign out</Link>
    </Box>
  );
}

/**
 * The public request page: the in-house way to ask for leave, overtime or a
 * carry-over/payout, with no Microsoft sign-in. Someone types their email and
 * proves they own it with an emailed code. A Staff Directory employee is then
 * signed in on this device for 30 days and their request goes straight to
 * their manager. Anyone else (a new hire not added yet) picks their
 * supervisor, and the request waits until that supervisor adds them.
 *
 * The page is split in two: a coloured panel with the steps, and the current
 * step beside it. Once past signing in, the page colour and the panel's words
 * follow the kind of request (see moods.ts).
 *
 * @returns The request page.
 */
export default function RequestPage() {
  const [step, setStep] = useState<Step>('email');
  const [type, setType] = useState<RequestType>('leave');
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [session, setSession] = useState<EmployeeSession | null>(null);
  const [verified, setVerified] = useState<VerifiedEmail | null>(null);
  const [values, setValues] = useState<RequestValues>({ ...EMPTY_REQUEST });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [resendIn, setResendIn] = useState(0);
  const [doneText, setDoneText] = useState('');
  const [heldWith, setHeldWith] = useState('');                // the supervisor a new hire's request waits on
  const [fromDashboard, setFromDashboard] = useState(false);   // signed in by the employee dashboard

  // New-hire details.
  const [supervisors, setSupervisors] = useState<Supervisor[]>([]);
  const [locations, setLocations] = useState<string[]>([]);
  const [name, setName] = useState('');
  const [location, setLocation] = useState('');
  const [supervisorId, setSupervisorId] = useState('');

  // A returning employee skips the code while their 30-day sign-in lasts.
  useEffect(() => {
    const saved = loadSession();
    if (saved) {
      setFromDashboard(isHandedOff());                 // opened from the employee dashboard in this tab
      setSession(saved);
      setEmail(saved.email);
      setStep('form');
    }
  }, []);

  // Count down to when a new code may be asked for.
  useEffect(() => {
    if (resendIn <= 0) return;
    const t = setTimeout(() => setResendIn((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [resendIn]);

  /** Update one form field. @param key - field. @param value - typed value. */
  const setValue = (key: keyof RequestValues, value: string) =>
    setValues((v) => ({ ...v, [key]: value }));

  /** Email a code to the address typed. */
  const sendCode = async () => {
    setError(''); setInfo(''); setBusy(true);
    try {
      const res = await requestCode(email.trim());
      setInfo(res.data.detail);                         // same text whoever they are
      setCode('');
      setResendIn(RESEND_SECONDS);
      setStep('code');
    } catch (err) {
      setError(errorText(err, 'The code could not be sent. Try again in a few minutes.'));
    } finally {
      setBusy(false);
    }
  };

  /** Check the code, then go to the employee form or the new-hire form. */
  const checkCode = async () => {
    setError(''); setBusy(true);
    try {
      const res = await verifyCode(email.trim(), code.trim());
      if (res.data.status === 'employee') {
        const signedIn: EmployeeSession = { ...res.data, email: email.trim().toLowerCase() };
        saveSession(signedIn);                          // 30 days on this device
        setSession(signedIn);
        setFromDashboard(false);
        setStep('form');
      } else {
        setVerified(res.data.verified);
        setStep('newhire');                             // the code is used up; never go back to it
        try {
          const options = await getSupervisors(res.data.verified);
          setSupervisors(options.data.supervisors || []);
          setLocations(options.data.locations || []);
        } catch {
          setError('The supervisor list could not be loaded. Refresh the page and start again.');
        }
      }
    } catch (err) {
      setError(errorText(err, 'That code could not be checked. Try again.'));
    } finally {
      setBusy(false);
    }
  };

  /** Submit as the signed-in employee. */
  const submitAsEmployee = async () => {
    if (!session) return;
    setError(''); setBusy(true);
    try {
      await submitMyRequest(session, type, buildRequestBody(type, values));
      setHeldWith('');
      setDoneText('Your request was sent to your manager. You will get an email when it is approved or rejected.');
      setStep('done');
    } catch (err: any) {
      if (err?.response?.status === 401) {              // the 30 days ran out
        signOut();
        setError('Your sign-in has expired. Enter your email to get a new code.');
      } else {
        setError(errorText(err, 'Your request could not be sent. Try again.'));
      }
    } finally {
      setBusy(false);
    }
  };

  /** Submit as a new hire: held until the supervisor adds them. */
  const submitAsNewHire = async () => {
    if (!verified) return;
    setError(''); setBusy(true);
    try {
      const res = await holdRequest({
        verified, name: name.trim(), location, supervisor_id: supervisorId,
        request_type: type, form: buildRequestBody(type, values),
      });
      setHeldWith(res.data.supervisor_name);
      setDoneText(
        `You are not in the Staff Directory yet, so we asked ${res.data.supervisor_name} to add you. `
        + 'Your request is sent to them automatically as soon as they do, and you will get an email.',
      );
      setStep('done');
    } catch (err: any) {
      if (err?.response?.status === 401 || err?.response?.status === 409) {
        setStep('email');                               // expired, or now on staff: start again
      }
      setError(errorText(err, 'Your request could not be sent. Try again.'));
    } finally {
      setBusy(false);
    }
  };

  /** Forget this device's sign-in and start over. */
  const signOut = () => {
    clearSession();
    setSession(null);
    setFromDashboard(false);
    setEmail('');
    setCode('');
    setStep('email');
  };

  /** Start another request, keeping the sign-in. */
  const another = () => {
    setValues({ ...EMPTY_REQUEST });
    setError('');
    setStep(session ? 'form' : 'email');
  };

  const emailOk = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
  const formReady = isRequestReady(type, values);
  const newHireReady = formReady && name.trim().split(/\s+/).length >= 2 && !!location && !!supervisorId;

  // The page colour follows the kind of request once the person is past signing in.
  const outer = useTheme();
  const mode = outer.palette.mode;
  const mood = moodOf(step === 'email' || step === 'code', type, values);
  const theme = useMemo(() => (mood ? buildTheme(mode, MOODS[mood][mode]) : outer), [mood, mode, outer]);
  const words = mood ? MOODS[mood].words : DEFAULT_WORDS;

  // The steps in the side panel: a new hire's third step is about them, not just the request.
  const held = step === 'newhire' || (step === 'done' && !session);
  const order: Step[] = ['email', 'code', held ? 'newhire' : 'form', 'done'];
  const at = order.indexOf(step);
  const steps: StepItem[] = order.map((k, i) => ({
    label: STEP_LABELS[k],
    state: i < at || (k === 'done' && step === 'done') ? 'done' : i === at ? 'on' : 'todo',
  }));

  // What happens next, shown once the request is sent or saved.
  const next: NextItem[] = held ? [
    { title: 'Saved', note: 'Just now', done: true },
    { title: `${heldWith || 'Your supervisor'} adds you to the staff list`, note: 'They got an email with a link' },
    { title: 'Your request goes to them', note: 'Automatically' },
    { title: 'You get an email with the decision' },
  ] : [
    { title: 'Sent to your manager', note: 'Just now', done: true },
    { title: 'They approve or reject', note: 'From the email, a text reply or their dashboard' },
    { title: 'You get an email', note: 'Your balance changes when it is approved' },
  ];

  const spinner = busy ? <CircularProgress size={16} color="inherit" /> : undefined;   // inside a busy button
  const big = { py: 1.5, fontSize: 15 } as const;                                       // the main buttons' size

  return (
    <ThemeProvider theme={theme}>
      <Box sx={{
        minHeight: '100dvh', bgcolor: 'background.default', color: 'text.primary',
        display: 'grid', gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: 'minmax(0, 1fr) minmax(0, 1.1fr)' },
        gridTemplateRows: { xs: 'auto 1fr', md: 'auto' },
      }}>
        <SidePanel words={words} steps={steps} />

        <Box component="main" sx={{ px: { xs: 2, sm: 5, md: 'clamp(16px, 5vw, 64px)' }, py: { xs: 3, md: 6 }, display: 'grid', alignContent: { xs: 'start', md: 'center' } }}>
          <Box
            key={step}                                  // a new step replays the entrance
            sx={{
              width: 'min(520px, 100%)', display: 'grid', gap: 2.25,
              // Each part rises in, one after another; off for people who ask for less motion.
              '@media (prefers-reduced-motion: no-preference)': {
                '& > *': { animation: 'rise .45s ease-out both' },
                '& > *:nth-of-type(2)': { animationDelay: '.05s' },
                '& > *:nth-of-type(3)': { animationDelay: '.1s' },
                '& > *:nth-of-type(n+4)': { animationDelay: '.15s' },
              },
              '@keyframes rise': { from: { opacity: 0, transform: 'translateY(8px)' } },
            }}
          >
            {step === 'email' && (
              <>
                <Heading>Make a request</Heading>
                <Lead>We email you a 6-digit code to confirm it is you.</Lead>
                {error && <Alert severity="error">{error}</Alert>}
                <TextField
                  label="Your email" type="email" autoComplete="email" autoFocus
                  placeholder="you@ucsh.com or your personal email"
                  value={email} onChange={(e) => setEmail(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && emailOk && !busy && sendCode()}
                />
                <Button variant="contained" size="large" sx={big} disabled={!emailOk || busy} onClick={sendCode} startIcon={spinner}>
                  Email me a code
                </Button>
              </>
            )}

            {step === 'code' && (
              <>
                <Heading>Check your email</Heading>
                <Lead>{info || `Sent to ${email.trim()}.`} Check your junk folder if it is not there.</Lead>
                {error && <Alert severity="error">{error}</Alert>}
                <CodeBoxes value={code} onChange={setCode} onEnter={() => code.length === 6 && !busy && checkCode()} />
                <Button variant="contained" size="large" sx={big} disabled={code.length !== 6 || busy} onClick={checkCode} startIcon={spinner}>
                  Continue
                </Button>
                <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
                  <Link component="button" variant="body2" onClick={() => { setStep('email'); setError(''); }}>Use a different email</Link>
                  <Link component="button" variant="body2" disabled={resendIn > 0 || busy} onClick={sendCode}
                    sx={{ '&:disabled': { color: 'text.disabled', cursor: 'default', textDecoration: 'none' } }}>
                    {resendIn > 0 ? `Send a new code (${resendIn}s)` : 'Send a new code'}
                  </Link>
                </Box>
              </>
            )}

            {step === 'form' && session && (
              <>
                <SignedInAs name={session.name} fromDashboard={fromDashboard} onSignOut={signOut} />
                {error && <Alert severity="error">{error}</Alert>}
                <TypeCards value={type} onPick={setType} />
                <RequestForm type={type} values={values} onChange={setValue} />
                <Button variant="contained" size="large" sx={big} disabled={!formReady || busy} onClick={submitAsEmployee} startIcon={spinner}>
                  Send request
                </Button>
                <Link href={dashboardHref(session)} variant="body2">My balances and past requests</Link>
              </>
            )}

            {step === 'newhire' && (
              <>
                <Heading>Tell us who you are</Heading>
                <Alert severity="info">
                  {email.trim()} is not in the Staff Directory yet. Tell us who you are and who your
                  supervisor is; they will be asked to add you, and your request goes to them once they do.
                </Alert>
                {error && <Alert severity="error">{error}</Alert>}
                <TextField label="Your first and last name" required value={name} onChange={(e) => setName(e.target.value)} />
                <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'repeat(2, minmax(0, 1fr))' } }}>
                  <TextField label="Your location" required select value={location} onChange={(e) => setLocation(e.target.value)}>
                    {locations.map((l) => <MenuItem key={l} value={l}>{l}</MenuItem>)}
                  </TextField>
                  <TextField label="Your supervisor" required select value={supervisorId}
                    onChange={(e) => setSupervisorId(e.target.value)}
                    helperText={supervisors.length === 0 ? 'No supervisors could be loaded. Try again later.' : undefined}>
                    {supervisors.map((s) => (
                      <MenuItem key={s.id} value={s.id}>{s.name}{s.location ? ` (${s.location})` : ''}</MenuItem>
                    ))}
                  </TextField>
                </Box>
                <TypeCards value={type} onPick={setType} />
                <RequestForm type={type} values={values} onChange={setValue} />
                <Button variant="contained" size="large" sx={big} disabled={!newHireReady || busy} onClick={submitAsNewHire} startIcon={spinner}>
                  Send request
                </Button>
              </>
            )}

            {step === 'done' && (
              <>
                {/* A tick that pops in. */}
                <Box sx={(t) => ({
                  width: 64, height: 64, borderRadius: '50%', display: 'grid', placeItems: 'center',
                  bgcolor: t.tokens.okSoft, color: 'success.main', fontSize: 30, fontWeight: 700,
                  '@media (prefers-reduced-motion: no-preference)': { animation: 'pop .5s cubic-bezier(.2,1.6,.4,1) both !important' },
                  '@keyframes pop': { from: { transform: 'scale(.4)', opacity: 0 } },
                })}>✓</Box>
                <Heading>{held ? 'Request saved' : 'Request sent'}</Heading>
                <Lead>{doneText}</Lead>
                <NextSteps items={next} />
                <Box sx={{ display: 'flex', gap: 1.25, flexWrap: 'wrap', alignItems: 'center' }}>
                  <Button variant="contained" onClick={another}>Make another request</Button>
                  {session && <Button variant="outlined" href={dashboardHref(session)}>My balances and past requests</Button>}
                </Box>
              </>
            )}
          </Box>
        </Box>
      </Box>
    </ThemeProvider>
  );
}
