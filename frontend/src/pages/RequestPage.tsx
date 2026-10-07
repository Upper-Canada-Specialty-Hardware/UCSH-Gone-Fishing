import { useEffect, useState } from 'react';
import {
  Box, Paper, Typography, TextField, Button, Alert, Stack, CircularProgress,
  ToggleButtonGroup, ToggleButton, MenuItem, Link,
} from '@mui/material';
import CheckCircleOutlineIcon from '@mui/icons-material/CheckCircleOutline';
import RequestForm, {
  EMPTY_REQUEST, REQUEST_TYPES, RequestValues, buildRequestBody, isRequestReady,
} from '../components/RequestForm';
import {
  EmployeeSession, RequestType, Supervisor, VerifiedEmail,
  clearSession, dashboardHref, errorText, getSupervisors, holdRequest,
  loadSession, requestCode, saveSession, submitMyRequest, verifyCode,
} from '../api/intake';

/**
 * Where the person is in the flow.
 * - email: pick a form, type your email
 * - code: type the 6-digit code that was emailed
 * - form: signed in as an employee; fill in the form
 * - newhire: email is on no staff record; say who you are, pick a supervisor, fill in the form
 * - done: submitted (or held for a new hire)
 */
type Step = 'email' | 'code' | 'form' | 'newhire' | 'done';

const RESEND_SECONDS = 60;   // wait before "send a new code" is offered

/**
 * The public request page: the in-house way to ask for leave, overtime or a
 * carry-over/payout, with no Microsoft sign-in. Someone types their email and
 * proves they own it with an emailed code. A Staff Directory employee is then
 * signed in on this device for 30 days and their request goes straight to
 * their manager. Anyone else (a new hire not added yet) picks their
 * supervisor, and the request waits until that supervisor adds them.
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

  /** The three-way form picker, shown above every form step. */
  const picker = (
    <ToggleButtonGroup
      exclusive fullWidth color="primary" value={type}
      onChange={(_, v) => v && setType(v)}
      sx={{ flexWrap: { xs: 'wrap', sm: 'nowrap' } }}
    >
      {REQUEST_TYPES.map((t) => (
        <ToggleButton key={t.value} value={t.value} sx={{ textTransform: 'none', flex: '1 1 30%' }}>
          {t.label}
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  );
  const hint = REQUEST_TYPES.find((t) => t.value === type)?.hint;

  return (
    <Box sx={{ display: 'flex', justifyContent: 'center' }}>
      <Paper sx={{ p: { xs: 2, sm: 4 }, width: '100%', maxWidth: 560 }}>
        <Typography variant="h5" sx={{ fontWeight: 600, mb: 0.5 }}>Make a request</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
          Leave, overtime, or a carry-over or payout. No Microsoft sign-in needed.
        </Typography>

        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

        {step === 'email' && (
          <Stack spacing={2}>
            {picker}
            <Typography variant="caption" color="text.secondary">{hint}</Typography>
            <TextField
              label="Your email" type="email" autoComplete="email" autoFocus
              value={email} onChange={(e) => setEmail(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && emailOk && !busy && sendCode()}
              helperText="We email you a 6-digit code to confirm it is you."
            />
            <Button variant="contained" size="large" disabled={!emailOk || busy} onClick={sendCode}
              startIcon={busy ? <CircularProgress size={16} color="inherit" /> : undefined}>
              Email me a code
            </Button>
          </Stack>
        )}

        {step === 'code' && (
          <Stack spacing={2}>
            {info && <Alert severity="info">{info}</Alert>}
            <TextField
              label="6-digit code" autoFocus value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              onKeyDown={(e) => e.key === 'Enter' && code.length === 6 && !busy && checkCode()}
              slotProps={{ htmlInput: { inputMode: 'numeric', autoComplete: 'one-time-code' } }}
              helperText={`Sent to ${email.trim()}. Check your junk folder if it is not there.`}
            />
            <Button variant="contained" size="large" disabled={code.length !== 6 || busy} onClick={checkCode}
              startIcon={busy ? <CircularProgress size={16} color="inherit" /> : undefined}>
              Continue
            </Button>
            <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
              <Button size="small" onClick={() => { setStep('email'); setError(''); }}>Use a different email</Button>
              <Button size="small" disabled={resendIn > 0 || busy} onClick={sendCode}>
                {resendIn > 0 ? `Send a new code (${resendIn}s)` : 'Send a new code'}
              </Button>
            </Box>
          </Stack>
        )}

        {step === 'form' && session && (
          <Stack spacing={2}>
            <Typography variant="body2">
              Signed in as <b>{session.name}</b>.{' '}
              <Link component="button" variant="body2" onClick={signOut}>Not you? Sign out</Link>
            </Typography>
            {picker}
            <RequestForm type={type} values={values} onChange={setValue} />
            <Button variant="contained" size="large" disabled={!formReady || busy} onClick={submitAsEmployee}
              startIcon={busy ? <CircularProgress size={16} color="inherit" /> : undefined}>
              Send request
            </Button>
            <Link href={dashboardHref(session)} variant="body2">My balances and past requests</Link>
          </Stack>
        )}

        {step === 'newhire' && (
          <Stack spacing={2}>
            <Alert severity="info">
              {email.trim()} is not in the Staff Directory yet. Tell us who you are and who your
              supervisor is; they will be asked to add you, and your request goes to them once they do.
            </Alert>
            <TextField label="Your first and last name" required value={name} onChange={(e) => setName(e.target.value)} />
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
            {picker}
            <RequestForm type={type} values={values} onChange={setValue} />
            <Button variant="contained" size="large" disabled={!newHireReady || busy} onClick={submitAsNewHire}
              startIcon={busy ? <CircularProgress size={16} color="inherit" /> : undefined}>
              Send request
            </Button>
          </Stack>
        )}

        {step === 'done' && (
          <Stack spacing={2} alignItems="center" sx={{ textAlign: 'center' }}>
            <CheckCircleOutlineIcon color="success" sx={{ fontSize: 56 }} />
            <Typography>{doneText}</Typography>
            <Button variant="outlined" onClick={another}>Make another request</Button>
            {session && <Link href={dashboardHref(session)} variant="body2">My balances and past requests</Link>}
          </Stack>
        )}
      </Paper>
    </Box>
  );
}
