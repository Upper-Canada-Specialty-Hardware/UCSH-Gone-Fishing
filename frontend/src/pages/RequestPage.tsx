import { ReactNode, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  Box, Typography, TextField, Button, Alert, CircularProgress, Link,
} from '@mui/material';
import { ThemeProvider, useTheme } from '@mui/material/styles';
import { AnimatePresence, m } from 'motion/react';
import RequestForm, {
  EMPTY_REQUEST, RequestValues, buildRequestBody, isRequestReady,
} from '../components/RequestForm';
import BalancePreview from '../components/request/BalancePreview';
import CodeBoxes from '../components/request/CodeBoxes';
import { NextItem, NextSteps, SidePanel, StepItem, TypeCards } from '../components/request/RequestLayout';
import { DEFAULT_WORDS, MOODS, moodOf } from '../components/request/moods';
import WhoYouAre, { EMPTY_STARTER, NewStarter, starterReady } from '../components/request/WhoYouAre';
import SupervisorPicker, { EMPTY_SUPERVISOR, SupervisorChoice, supervisorReady } from '../components/request/SupervisorPicker';
import { buildTheme } from '../theme';
import { carryoverClosedReason } from '../utils/carryoverWindow';
import { isHourly } from '../utils/employment';
import { getMyBalances } from '../api/client';
import { useSessionDraft } from '../hooks/useSessionDraft';
import { AutoHeight, DrawnTick, EXIT, Fold, INSTANT, KF, SPRING, staggerChildren, useCalm, useDirection } from '../components/Motion';
import {
  Balances, BookingLimit, EmployeeSession, HeldGate, HeldStatus, HeldStatusResult, RequestProgress, RequestType, SignInChoice, VerifiedEmail,
  clearHeldWaiting, clearSession, errorText, getHeldStatus, getRequestStatus, getRequestTypes, heldSignIn, holdRequest,
  loadHeldWaiting, loadNewHire, loadSession, repick, requestCode, saveHeldWaiting, saveNewHire, saveSession,
  setSignInNotice, submitMyRequest, takeAfterSignIn, takeSignInNotice, verifyCode,
} from '../api/intake';

/**
 * Where the person is in the flow.
 * - email: type your email
 * - code: type the 6-digit code that was emailed
 * - form: signed in as an employee; pick a form and fill it in
 * - newhire: email is on no staff record; say who you are and who your supervisor is
 * - newhire-request: then pick a form and fill it in
 * - held-status: a request is waiting to be set up; shows where it stands and, when
 *   a supervisor handed it back, lets the person pick another supervisor (#442, #443)
 * - done: submitted (or held for a new hire)
 */
type Step = 'email' | 'code' | 'choose' | 'form' | 'newhire' | 'newhire-request'
  | 'held-status' | 'done';

const RESEND_SECONDS = 60;   // wait before "send a new code" is offered
const HELD_POLL_MS = 3000;   // how often a new hire's waiting page asks whether their request went through
const STATUS_POLL_MS = 3000; // how often a sent request's page asks how it is going (#364)
const STATUS_POLL_CAP_MS = 30 * 60 * 1000;   // stop following after half an hour, even if nothing decided it

/**
 * A timestamp as a short local clock time (e.g. "2:45 PM"), shown beside a
 * finished step.
 *
 * @param iso - The ISO 8601 time the step happened, or null while it waits.
 * @returns The clock time, or undefined when there is no usable time.
 */
function stepTime(iso: string | null): string | undefined {
  if (!iso) return undefined;                        // still waiting: no time to show
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return undefined;   // unparseable: show nothing
  return when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

/**
 * The title for the balance step once it is done, by how the balance moved.
 *
 * @param effect - "applied", "counted", "no_balance_used", or null.
 * @returns A plain-words title.
 */
function balanceTitle(effect: 'applied' | 'counted' | 'no_balance_used' | null): string {
  if (effect === 'counted') return 'Your vacation was counted';       // hourly vacation: a counter, no pot moves
  if (effect === 'no_balance_used') return 'No balance was used';      // bereavement, jury duty, hourly overtime
  return 'Your balance was updated';                                   // a pot was spent or credited
}

/**
 * The "what happens next" points for a signed-in employee's request, driven by
 * the live progress once it is known (#364). Before the first poll answers
 * (progress is null), it reads as the plain just-sent list.
 *
 * @param progress - The latest status from the server, or null before the first poll.
 * @returns The points, in order, each with its done state and time.
 */
function staffNextSteps(progress: RequestProgress | null): NextItem[] {
  const manager = progress?.manager_name || 'your manager';   // filled in once the send names them
  const steps = progress?.steps;
  const result = steps?.decided.result ?? null;
  const decided = steps?.decided.done;                         // a decision has landed
  // The ask reaches a manager only once the sent_to_manager event lands; a request
  // sent while every manager is off waits on the admin cover list and this step is
  // not yet done (#370). Before the first poll answers (no steps) assume the common
  // case (a manager was asked); once a decision has landed the ask is moot, so show
  // it done either way.
  let sent: NextItem;
  if (!steps) {
    sent = { title: `Sent to ${manager}`, note: 'Just now', done: true };   // optimistic until the poll answers
  } else if (steps.sent_to_manager.done || decided) {
    sent = { title: `Sent to ${manager}`, note: stepTime(steps.sent_to_manager.at) || 'Just now', done: true };
  } else {
    // No manager was asked yet: everyone is off, so it waits until one is back.
    sent = { title: 'Waiting for a manager to be available', note: 'No manager is free right now', done: false };
  }
  // Name who decided: "an admin" or the manager who acted (a stand-in when the
  // assigned manager was off), falling back to the assigned manager (#371).
  const decider = steps?.decided.decider || manager;
  const decidedItem: NextItem = decided
    ? {
        title: result === 'rejected' ? `${decider} rejected it` : `${decider} approved it`,
        note: stepTime(steps!.decided.at),
        done: true,
      }
    : { title: 'They approve or reject', note: 'From the email, a text reply or their dashboard' };
  const emailed: NextItem = steps?.result_sent.done
    ? { title: 'You got an email with the decision', note: stepTime(steps.result_sent.at), done: true }
    : { title: 'You get an email with the decision' };
  // A rejection moves no balance, so the balance point is left out once rejected.
  if (result === 'rejected') return [sent, decidedItem, emailed];
  const balance: NextItem = steps?.balance.done
    ? { title: balanceTitle(steps.balance.effect), note: stepTime(steps.balance.at), done: true }
    : { title: 'Your balance updates', note: 'When it is approved' };
  return [sent, decidedItem, balance, emailed];
}

/** The steps in the order they come, for which way a change of step slides. */
const STEP_ORDER: Step[] = ['email', 'code', 'choose', 'form', 'held-status', 'done'];

/** A new step slides in from the side the person moved towards; the old one leaves the other way. */
const STEP_MOTION = {
  enter: (dir: number) => ({ opacity: 0, x: dir * 24 }),
  show: { opacity: 1, x: 0, transition: SPRING },
  leave: (dir: number) => ({ opacity: 0, x: dir * -24, transition: EXIT }),
};

/**
 * A button's label with room made for a spinner while it works: the spinner
 * opens in beside it and the label slides over instead of jumping.
 *
 * @param props.busy - Whether the button is working.
 * @param props.children - The label.
 * @returns The label.
 */
function BusyLabel({ busy, children }: { busy: boolean; children: ReactNode }) {
  const calm = useCalm();
  return (
    <>
      <AnimatePresence initial={false}>
        {busy && (
          <m.span
            key="spin"
            initial={{ opacity: 0, scale: 0.4, width: 0 }}
            animate={{ opacity: 1, scale: 1, width: 24 }}
            exit={{ opacity: 0, scale: 0.4, width: 0 }}
            transition={calm ? INSTANT : undefined}
            style={{ display: 'inline-flex', overflow: 'hidden', flex: 'none' }}
          >
            <CircularProgress size={16} color="inherit" />
          </m.span>
        )}
      </AnimatePresence>
      <span>{children}</span>
    </>
  );
}

/**
 * A small ring that empties over the wait before a new code may be asked for.
 * Hidden when less motion is asked for; the seconds in the link still count.
 *
 * @returns The ring.
 */
function ResendRing() {
  return (
    <Box component="svg" viewBox="0 0 16 16" aria-hidden
      sx={{ width: 14, height: 14, flex: 'none', transform: 'rotate(-90deg)', '@media (prefers-reduced-motion: reduce)': { display: 'none' } }}>
      <Box component="circle" cx={8} cy={8} r={6} sx={{ fill: 'none', stroke: (t) => t.palette.divider, strokeWidth: 2.5 }} />
      <Box component="circle" cx={8} cy={8} r={6} pathLength={1}
        sx={{ fill: 'none', stroke: (t) => t.palette.primary.main, strokeWidth: 2.5, strokeDasharray: 1, animation: `${KF.empty} ${RESEND_SECONDS}s linear both` }} />
    </Box>
  );
}

/** Each step's name in the side panel. */
const STEP_LABELS: Record<Step, string> = {
  email: 'Your email',
  code: 'The code we email you',
  choose: 'Which person you are',
  form: 'Your request',
  newhire: 'About you',
  'newhire-request': 'Your request',
  'held-status': 'Where it stands',
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
 * The sign-in, request and new-hire pages: the in-house way to ask for leave,
 * overtime or a carry-over/payout, with no Microsoft sign-in. It serves three
 * addresses:
 * - #/ (the public landing page): someone types their email and proves they
 *   own it with an emailed code. A Staff Directory employee is then signed in
 *   on this device for 30 days and moves on to My requests (or the page they
 *   had opened). Anyone else moves on to #/new-hire.
 * - #/my/request: a signed-in employee's new request, which goes straight to
 *   their manager.
 * - #/new-hire: someone not on staff yet says who they are and who their
 *   supervisor is, then makes the request, which waits until that
 *   supervisor adds them.
 *
 * The page is split in two: a coloured panel with the steps, and the current
 * step beside it. Once past signing in, the page colour and the panel's words
 * follow the kind of request (see moods.ts).
 *
 * @returns The request page.
 */
/** Shown on the carry-over card until the server answers, or when it cannot be reached. */
const CARRYOVER_UNKNOWN = 'Not open right now.';

export default function RequestPage() {
  const [step, setStep] = useState<Step>('email');
  const [type, setType] = useState<RequestType>('leave');
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [session, setSession] = useState<EmployeeSession | null>(null);
  const [verified, setVerified] = useState<VerifiedEmail | null>(null);
  const [choices, setChoices] = useState<SignInChoice[]>([]);   // the records on a shared mailbox, to pick from
  const [values, setValues] = useState<RequestValues>({ ...EMPTY_REQUEST });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [resendIn, setResendIn] = useState(0);
  const [doneText, setDoneText] = useState('');
  const [heldWith, setHeldWith] = useState('');                // the supervisor a new hire's request waits on
  const [heldToken, setHeldToken] = useState('');              // signed token to ask how the held request is going
  const [heldStatus, setHeldStatus] = useState<HeldStatus>('waiting');   // what that request is doing now
  // The fields the status page needs to let a new hire re-pick a supervisor after
  // a "not on my team" (#443).
  const [heldSummary, setHeldSummary] = useState('');          // the request in words, shown while re-picking
  const [heldDept, setHeldDept] = useState('');                // the department to search a new supervisor in
  const [repickChoice, setRepickChoice] = useState<SupervisorChoice>(EMPTY_SUPERVISOR);   // the re-pick
  const [myToken, setMyToken] = useState('');                  // signed token to follow a sent request's progress (#364)
  const [progress, setProgress] = useState<RequestProgress | null>(null);   // that request's latest progress
  // A held request was released but the automatic sign-in was refused (#482):
  // the page still follows the request and offers a plain email sign-in link.
  const [heldSignedOut, setHeldSignedOut] = useState(false);
  const [notice, setNotice] = useState('');                    // why someone was sent to the landing page
  // Why carry-over and payout are greyed out, or null when open. Closed until the server says otherwise.
  const [carryoverClosed, setCarryoverClosed] = useState<string | null>(CARRYOVER_UNKNOWN);
  // The carry-over/payout window HR set (ISO dates), or null; shown in the policy above that form (#429).
  const [carryoverWindow, setCarryoverWindow] = useState<{ start: string; end: string } | null>(null);
  const [myBalances, setMyBalances] = useState<Balances | null>(null);  // signed-in employee's balances (#300)
  const [bookingLimit, setBookingLimit] = useState<BookingLimit | null>(null);  // vacation booking limit from the preview (#407)
  const hourlyStaff = isHourly(myBalances);                     // hourly: forms and summary adapt
  const [sends, setSends] = useState(0);                       // codes sent, to restart the resend ring
  const [misses, setMisses] = useState(0);                     // refused codes, to shake the boxes
  const navigate = useNavigate();
  // Keep the typed request across a #331 auto-update reload: save the form values
  // and restore them once on mount. clearDraft forgets them after a submit.
  const { clearDraft } = useSessionDraft<RequestValues>(
    'request-form-draft', values, (saved) => setValues({ ...EMPTY_REQUEST, ...saved }),
  );
  const { pathname } = useLocation();
  const newHirePage = pathname === '/new-hire';               // the #131 path for people not on staff
  const signedInPage = pathname.startsWith('/my/request');    // under the top bar with the page tabs

  // What a new starter says about themselves (#226).
  const [starter, setStarter] = useState<NewStarter>(EMPTY_STARTER);

  // Carry-over and payout are only taken between the dates an admin set (#176).
  useEffect(() => {
    getRequestTypes()
      .then(({ data }) => {
        setCarryoverClosed(
          carryoverClosedReason(data.carryover_payout_open, data.carryover_payout_window, data.today),
        );
        setCarryoverWindow(data.carryover_payout_window);    // the dates HR set, for the policy (#429)
      })
      .catch(() => setCarryoverClosed(CARRYOVER_UNKNOWN));    // unknown: do not offer them
  }, []);
  // A closed form (or an hourly employee, who has no vacation balance to move)
  // cannot stay picked (e.g. closed while the page was open).
  useEffect(() => {
    if ((carryoverClosed || hourlyStaff) && type === 'carryover-payout') setType('leave');
  }, [carryoverClosed, hourlyStaff, type]);
  // hourly cannot carry over or pay out at all; otherwise the card is closed only outside HR's dates.
  const closedTypes: Partial<Record<RequestType, string>> = hourlyStaff
    ? { 'carryover-payout': 'Not for hourly staff. Vacation is tracked, not a balance.' }
    : carryoverClosed ? { 'carryover-payout': carryoverClosed } : {};

  // Pick up where this address starts: a held request still being followed, a
  // new hire's verified email, the shared sign-in for a new request, or the
  // landing page's note.
  useEffect(() => {
    // A held request this device is still following returns to its status page
    // instead of the email step or the new-hire form, so a reload resumes (#482).
    const waitingToken = loadHeldWaiting();
    if (waitingToken && !loadSession()) {
      const v = loadNewHire();                       // the re-pick needs this when the tab still has it
      if (v) { setVerified(v); setEmail(v.email); }
      setHeldToken(waitingToken);
      setHeldStatus('waiting');                      // the poll corrects it to sent/cancelled/not_on_team
      setStep('held-status');
      return;
    }
    if (newHirePage) {
      const v = loadNewHire();
      if (!v) {                                        // no verified email in this tab (or it expired)
        navigate('/', { replace: true });
        return;
      }
      setVerified(v);
      setEmail(v.email);
      setStep('newhire');                              // who you are comes first
      return;
    }
    const saved = loadSession();
    if (saved) {
      setSession(saved);
      setEmail(saved.email);
      setStep('form');
      return;
    }
    const note = takeSignInNotice();
    if (note) setNotice(note);                         // read once; a second run finds nothing
  }, [newHirePage]);

  // Once signed in, read this employee's balances: an hourly employee gets the
  // sick-days panel and adapted forms (#300). Read-only and harmless for new hires
  // (no session), who are never hourly here. Read again after each request is
  // sent (doneText changes), so the panel shows the new numbers next time.
  useEffect(() => {
    if (!session) { setMyBalances(null); return; }
    let live = true;
    getMyBalances()
      .then((r) => { if (live) setMyBalances(r.data.balances); })
      .catch(() => { if (live) setMyBalances(null); });         // unknown: treat as not hourly
    return () => { live = false; };
  }, [session, doneText]);

  // A new hire's waiting page follows its held request: ask every few seconds,
  // and once the supervisor adds them (or an admin cancels it) say so at once.
  useEffect(() => {
    if (step !== 'done' || !heldToken || heldStatus !== 'waiting') return;   // nothing to follow
    let live = true;
    /** Ask once; a failed ask is simply tried again on the next tick. */
    const ask = () => {
      if (document.hidden) return;                     // a background tab waits until it is looked at
      getHeldStatus(heldToken)
        .then(({ data }) => {
          if (!live || data.status === 'waiting') return;
          if (data.status === 'sent') { void handleHeldSent(data); return; }   // released: follow + sign in (#482)
          if (data.status === 'refused') {
            // The release was refused on a booking rule: final, say why (#406).
            setHeldWith(data.supervisor_name);
            setHeldStatus('refused');
            clearHeldWaiting();                        // do not resume the held page after a reload
            setDoneText(data.reason || 'Your request could not be sent.');
            return;
          }
          // Cancelled: final, and stop resuming the held page after a reload.
          setHeldWith(data.supervisor_name);
          setHeldStatus(data.status);
          clearHeldWaiting();
          setDoneText('This request was cancelled. If you still need it, start again with your email.');
        })
        .catch(() => { /* retried on the next tick */ });
    };
    const timer = setInterval(ask, HELD_POLL_MS);
    document.addEventListener('visibilitychange', ask); // coming back to the tab asks straight away
    return () => {
      live = false;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', ask);
    };
  }, [step, heldToken, heldStatus]);

  // The held-status page (#442, #443) follows a waiting request: a new hire who
  // just made one, or anyone who signed in with one still waiting. It keeps asking
  // while the request is waiting or handed back ("not_on_team"), and stops once it
  // is sent or cancelled. A not_on_team answer also carries the department and
  // summary the re-pick control needs.
  useEffect(() => {
    if (step !== 'held-status' || !heldToken) return;
    if (heldStatus === 'sent' || heldStatus === 'cancelled' || heldStatus === 'refused') return;   // final: nothing to follow
    let live = true;
    /** Ask once; a failed ask is simply tried again on the next tick. */
    const ask = () => {
      if (document.hidden) return;                     // a background tab waits until it is looked at
      getHeldStatus(heldToken)
        .then(({ data }) => {
          if (!live) return;
          if (data.status === 'not_on_team') {
            // Handed back: show who declined it and what the re-pick needs.
            setHeldStatus('not_on_team');
            setHeldWith(data.declined_by || data.supervisor_name);
            setHeldDept(data.department || '');
            setHeldSummary(data.summary || '');
            return;
          }
          if (data.status === 'waiting') {
            setHeldWith(data.supervisor_name);          // the supervisor it waits on may have changed
            setHeldStatus('waiting');
            return;
          }
          // Sent, refused or cancelled: final.
          if (data.status === 'sent') { void handleHeldSent(data); return; }   // released: follow + sign in (#482)
          if (data.status === 'refused') {
            // The release was refused on a booking rule: final, say why (#406).
            setHeldWith(data.supervisor_name);
            setHeldStatus('refused');
            clearHeldWaiting();
            setDoneText(data.reason || 'Your request could not be sent.');
            return;
          }
          setHeldWith(data.supervisor_name);
          setHeldStatus(data.status);                   // cancelled
          clearHeldWaiting();                           // stop resuming the held page after a reload
          setDoneText('This request was cancelled. If you still need it, start again with your email.');
        })
        .catch(() => { /* retried on the next tick */ });
    };
    ask();                                              // ask straight away, so a re-pick's department fills fast
    const timer = setInterval(ask, HELD_POLL_MS);
    document.addEventListener('visibilitychange', ask);
    return () => {
      live = false;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', ask);
    };
  }, [step, heldToken, heldStatus]);

  // A signed-in employee's sent request follows itself the same way (#364): ask
  // every few seconds while the done page is open, so the steps fill in as the
  // manager approves or rejects by email, text or dashboard. It stops once the
  // request is finished, and gives up after a cap so it never polls forever.
  useEffect(() => {
    // Follows a request whenever the done page holds its token: a signed-in
    // employee's own request, and a released held request after sign-in (even a
    // refused one, so the steps still fill in without a session) (#364, #482).
    if (step !== 'done' || !myToken || progress?.finished) return;   // nothing to follow
    let live = true;
    let timer: ReturnType<typeof setInterval>;
    const deadline = Date.now() + STATUS_POLL_CAP_MS;   // half an hour from when following began
    /** Ask once; a failed or refused ask simply leaves the steps as they are. */
    const ask = () => {
      if (!live) return;
      if (Date.now() > deadline) { clearInterval(timer); return; }   // past the cap: stop following
      if (document.hidden) return;                      // a background tab waits until it is looked at
      getRequestStatus(myToken)
        .then(({ data }) => { if (live) setProgress(data); })
        .catch(() => { /* retried on the next tick; a 401/404 just keeps the plain steps */ });
    };
    ask();                                              // ask straight away, not only after the first interval
    timer = setInterval(ask, STATUS_POLL_MS);
    document.addEventListener('visibilitychange', ask); // coming back to the tab asks straight away
    return () => {
      live = false;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', ask);
    };
  }, [step, session, myToken, progress?.finished]);

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
      setSends((n) => n + 1);
      setStep('code');
    } catch (err) {
      setError(errorText(err, 'The code could not be sent. Try again in a few minutes.'));
    } finally {
      setBusy(false);
    }
  };

  /** Check the code, then move on: staff to their pages, anyone else to #/new-hire. */
  const checkCode = async () => {
    setError(''); setBusy(true);
    try {
      const res = await verifyCode(email.trim(), code.trim());
      if (res.data.status === 'employee') {
        // One request until set up (#442): when this person already has a request
        // waiting to be set up, show its status instead of signing them in to a
        // new form; otherwise sign in as usual.
        if (res.data.held) enterHeldStatus(res.data.held, res.data.verified);
        else signInAs(res.data);
      } else if (res.data.status === 'choose') {
        setChoices(res.data.people);                    // a shared mailbox: ask which record this is
        setStep('choose');
      } else if (res.data.held) {
        // A new hire who already has a request waiting sees its status (#442).
        enterHeldStatus(res.data.held, res.data.verified);
      } else {
        saveNewHire(res.data.verified);                 // the code is used up; this tab keeps the 24-hour pass
        navigate('/new-hire', { replace: true });
      }
    } catch (err) {
      setError(errorText(err, 'That code could not be checked. Try again.'));
      setMisses((n) => n + 1);
    } finally {
      setBusy(false);
    }
  };

  /**
   * Keep the signed-in record on this device and go on to the person's pages.
   *
   * @param person - The record signed in as: the only one on the address, or the one picked.
   */
  const signInAs = (person: Omit<EmployeeSession, 'email'>) => {
    const signedIn: EmployeeSession = {
      name: person.name, role: person.role, uid: person.uid, token: person.token, exp: person.exp,
      email: email.trim().toLowerCase(),
    };
    saveSession(signedIn);                              // 30 days on this device
    navigate(takeAfterSignIn() || '/my', { replace: true });   // back to a bookmarked page, or My requests
  };

  /** Submit as the signed-in employee. */
  const submitAsEmployee = async () => {
    if (!session) return;
    setError(''); setBusy(true);
    try {
      const res = await submitMyRequest(session, type, buildRequestBody(type, values));
      clearDraft();                                   // submitted: no draft to keep
      setHeldWith('');
      setHeldToken('');                               // a staff request has nothing to wait on
      setMyToken(res.data.status_token || '');        // follow this request's progress (#364)
      setProgress(null);                              // the first poll fills it in
      setDoneText('Your request was sent to your manager. You will get an email when it is approved or rejected.');
      setStep('done');
    } catch (err: any) {
      if (err?.response?.status === 401) {              // the 30 days ran out
        clearSession();
        setSignInNotice('Your sign-in has ended. Enter your email for a new code.');
        navigate('/', { replace: true });
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
        verified,
        first_name: starter.first.trim(),
        last_name: starter.last.trim(),
        department: starter.department,
        // The search pick, or the typed work email when the search did not find them.
        ...(starter.useEmail
          ? { supervisor_email: starter.supervisorEmail.trim() }
          : { supervisor_id: starter.supervisor?.id }),
        request_type: type, form: buildRequestBody(type, values),
      });
      clearDraft();                                     // held: no draft to keep
      setHeldWith(res.data.supervisor_name);
      setHeldToken(res.data.status_token);              // lets the page follow it from here
      saveHeldWaiting(res.data.status_token);           // resume the status page after a reload (#482)
      setHeldStatus('waiting');
      saveNewHire(null);                                // held: a refresh must not send it twice
      setDoneText(
        `You are not in the Staff Directory yet, so we asked ${res.data.supervisor_name} to add you. `
        + 'Your request is sent to them automatically as soon as they do, and you will get an email.',
      );
      setStep('done');
    } catch (err: any) {
      if (err?.response?.status === 401 || err?.response?.status === 409) {
        // Expired, or on staff now: start again from the landing page, which says why.
        saveNewHire(null);
        setSignInNotice(errorText(err, 'Start again with your email.'));
        navigate('/', { replace: true });
        return;
      }
      setError(errorText(err, 'Your request could not be sent. Try again.'));
    } finally {
      setBusy(false);
    }
  };

  /**
   * A held request has just been released ("sent"): follow the real request, and
   * treat the release as a sign-in for a short window so the person moves onto
   * their signed-in pages without a new code (#482). A refused sign-in still
   * follows the request and shows a plain email sign-in link.
   *
   * @param data - The held status answer that reported "sent".
   */
  const handleHeldSent = async (data: HeldStatusResult) => {
    setHeldWith(data.supervisor_name);
    setHeldStatus('sent');                              // final: the polling effects stop
    setMyToken(data.request_status_token || '');        // follow the real request from here (#364)
    setProgress(null);                                  // the first request poll fills it in
    setDoneText(
      `${data.supervisor_name || 'Your supervisor'} added you to the staff list, so your request was sent to them. `
      + 'You will get an email when it is approved or rejected.',
    );
    setStep('done');                                    // both held flows converge on the done page
    try {
      // The release signs them in: the supervisor confirmed who they are and this
      // device proved the address, so a staff session comes back (#482).
      const { data: signed } = await heldSignIn(heldToken);
      const session: EmployeeSession = {
        name: signed.name, role: signed.role, uid: signed.uid,
        token: signed.token, exp: signed.exp, email: signed.email,
      };
      saveSession(session);                             // 30 days on this device
      setSession(session);
      setMyToken(signed.request_status_token || data.request_status_token || '');
      setHeldSignedOut(false);
      clearHeldWaiting();                               // signed in: stop resuming the held page
    } catch {
      // The window closed or the record is gone: keep following the request and
      // offer a plain email sign-in instead.
      setHeldSignedOut(true);
    }
  };

  /**
   * Show the status of a request already waiting to be set up, instead of a new
   * form (#442). Reached when /verify says the person has one in flight, so they
   * cannot stack a second request and can re-pick a handed-back one.
   *
   * @param gate - The held summary /verify returned.
   * @param verifiedToken - The verified-email token /verify returned, for the re-pick.
   */
  const enterHeldStatus = (gate: HeldGate, verifiedToken?: VerifiedEmail) => {
    if (verifiedToken) setVerified(verifiedToken);     // lets the status page re-pick a supervisor
    setHeldToken(gate.status_token);                   // the page follows the request with this
    saveHeldWaiting(gate.status_token);                // resume this held page after a reload (#482)
    setHeldWith(gate.supervisor_name);
    // "not_on_team" means a supervisor handed it back, so the page offers a re-pick.
    setHeldStatus(gate.state === 'not_on_team' ? 'not_on_team' : 'waiting');
    setRepickChoice({ ...EMPTY_SUPERVISOR });
    setStep('held-status');
  };

  /** Pick another supervisor after a "not on my team", then keep following it (#443). */
  const submitRepick = async () => {
    if (!verified) return;
    setError(''); setBusy(true);
    try {
      const res = await repick({
        verified,
        status_token: heldToken,
        ...(repickChoice.useEmail
          ? { supervisor_email: repickChoice.email.trim() }
          : { supervisor_id: repickChoice.supervisor?.id }),
      });
      setHeldWith(res.data.supervisor_name);
      setHeldToken(res.data.status_token);              // refreshed token for the same request
      saveHeldWaiting(res.data.status_token);           // keep resuming with the refreshed token (#482)
      setHeldStatus('waiting');                         // waiting on the new supervisor now
      setRepickChoice({ ...EMPTY_SUPERVISOR });
    } catch (err: any) {
      if (err?.response?.status === 401) {              // the email check expired
        setSignInNotice(errorText(err, 'Start again with your email.'));
        navigate('/', { replace: true });
        return;
      }
      setError(errorText(err, 'That did not work. Try another supervisor.'));
    } finally {
      setBusy(false);
    }
  };

  /** Start another request, keeping the sign-in; a new hire starts again from the landing page. */
  const another = () => {
    if (!session) {
      navigate('/', { replace: true });
      return;
    }
    setValues({ ...EMPTY_REQUEST });
    clearDraft();                                       // fresh form: drop the old draft
    setMyToken('');                                     // stop following the request just finished (#364)
    setProgress(null);
    setError('');
    setStep('form');
  };

  const emailOk = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
  const formReady = isRequestReady(type, values);
  const newHireReady = formReady && starterReady(starter);

  // The page colour follows the kind of request once the person is past signing in.
  const outer = useTheme();
  const mode = outer.palette.mode;
  const mood = moodOf(step === 'email' || step === 'code', type, values);
  const theme = useMemo(() => (mood ? buildTheme(mode, MOODS[mood][mode]) : outer), [mood, mode, outer]);
  const words = mood ? MOODS[mood].words : DEFAULT_WORDS;

  // The steps in the side panel: a new hire's third step is about them, not just
  // the request (#440).
  const held = step === 'newhire' || step === 'newhire-request' || (step === 'done' && !session);
  // The status page reached by signing in: just email, code, then here.
  const gateStatus = step === 'held-status';
  // The status page sits in the panel's last ("done") slot while it follows the request.
  const panelStep: Step = step === 'held-status' ? 'done' : step;
  const order: Step[] = held
    ? ['email', 'code', 'newhire', 'newhire-request', 'done']
    : gateStatus
      ? ['email', 'code', 'done']
      : panelStep === 'choose' ? ['email', 'code', 'choose', 'form', 'done'] : ['email', 'code', 'form', 'done'];   // the pick shows only when asked
  const at = order.indexOf(panelStep);
  const steps: StepItem[] = order.map((k, i) => ({
    label: STEP_LABELS[k],
    // The real "done" step is complete; the status page is the current step, not done.
    state: i < at || (k === 'done' && step === 'done') ? 'done' : i === at ? 'on' : 'todo',
  }));

  // What happens next, shown once the request is sent or saved.
  // A new hire's list moves on by itself once their supervisor adds them.
  const added = heldStatus === 'sent';
  // While still waiting, show the new-hire steps; once added (or for a signed-in
  // employee) follow the real request's progress to the decision (#364, #482).
  // A refusal is final and never goes to a supervisor, so it shows no next
  // steps: the reason (in doneText) says what to do instead (#406).
  const next: NextItem[] = heldStatus === 'refused' ? [] : (held && !added) ? [
    { title: 'Saved', note: 'Just now', done: true },
    { title: `${heldWith || 'Your supervisor'} adds you to the staff list`, note: 'They got an email with a link' },
    { title: 'Your request goes to them', note: 'Automatically' },
    { title: 'You get an email with the decision' },
  ] : staffNextSteps(progress);

  const dir = useDirection(step === 'newhire' ? 2 : STEP_ORDER.indexOf(step));
  const big = { py: 1.5, fontSize: 15 } as const;                                       // the main buttons' size

  return (
    <ThemeProvider theme={theme}>
      <Box sx={{
        // Under the top bar the page fills what is left of the screen.
        minHeight: signedInPage ? { xs: 'auto', md: 'calc(100dvh - 56px)' } : '100dvh', bgcolor: 'background.default', color: 'text.primary',
        display: 'grid', gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: 'minmax(0, 1fr) minmax(0, 1.1fr)' },
        gridTemplateRows: { xs: 'auto 1fr', md: 'auto' },
      }}>
        {/* Signed in, the top bar already shows the name, and no sign-in steps are left to show. */}
        <SidePanel words={words} steps={signedInPage ? [] : steps} brand={!signedInPage} glow={mood ? MOODS[mood].glow : undefined} />

        <Box component="main" sx={{ px: { xs: 2, sm: 5, md: 'clamp(16px, 5vw, 64px)' }, py: { xs: 3, md: 6 }, display: 'grid', alignContent: { xs: 'start', md: 'center' } }}>
          <AnimatePresence mode="wait" initial={false} custom={dir}>
          <m.div key={step} custom={dir} variants={STEP_MOTION} initial="enter" animate="show" exit="leave" style={{ width: 'min(520px, 100%)', minWidth: 0 }}>
          <Box
            sx={{
              display: 'grid', gap: 2.25,
              // Each part rises in, one after another.
              ...staggerChildren({ step: 50, max: 3, dur: 450 }),
            }}
          >
            {step === 'email' && (
              <>
                <Fold show={!!notice}><Alert severity="info">{notice}</Alert></Fold>
                <Heading>Sign in</Heading>
                <Lead>We email you a 6-digit code to confirm it is you. You then stay signed in on this device for 30 days.</Lead>
                <Fold show={!!error}><Alert severity="error">{error}</Alert></Fold>
                <TextField
                  label="Your email" type="email" autoComplete="email" autoFocus
                  placeholder="you@ucsh.com or your personal email"
                  value={email} onChange={(e) => setEmail(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && emailOk && !busy && sendCode()}
                />
                <Button variant="contained" size="large" sx={big} disabled={!emailOk || busy} onClick={sendCode}>
                  <BusyLabel busy={busy}>Email me a code</BusyLabel>
                </Button>
                <Typography variant="body2" color="text.secondary">
                  New and no work email yet? Use your personal email. Your supervisor is asked to add you.
                </Typography>
              </>
            )}

            {step === 'code' && (
              <>
                <Heading>Check your email</Heading>
                <Lead>{info || `Sent to ${email.trim()}.`} Check your junk folder if it is not there.</Lead>
                <Fold show={!!error}><Alert severity="error">{error}</Alert></Fold>
                <CodeBoxes value={code} onChange={setCode} onEnter={() => code.length === 6 && !busy && checkCode()} misses={misses} />
                <Button variant="contained" size="large" sx={big} disabled={code.length !== 6 || busy} onClick={checkCode}>
                  <BusyLabel busy={busy}>Continue</BusyLabel>
                </Button>
                <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap', alignItems: 'center' }}>
                  <Link component="button" variant="body2" onClick={() => { setStep('email'); setError(''); }}>Use a different email</Link>
                  <Box sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.75 }}>
                    {resendIn > 0 && <ResendRing key={sends} />}
                    <Link component="button" variant="body2" disabled={resendIn > 0 || busy} onClick={sendCode}
                      sx={{ fontVariantNumeric: 'tabular-nums', '&:disabled': { color: 'text.disabled', cursor: 'default', textDecoration: 'none' } }}>
                      {resendIn > 0 ? `Send a new code (${resendIn}s)` : 'Send a new code'}
                    </Link>
                  </Box>
                </Box>
              </>
            )}

            {step === 'choose' && (
              <>
                <Heading>Which person are you?</Heading>
                <Lead>More than one staff record uses {email.trim()}. Pick yours: your requests and balances follow it.</Lead>
                <Box sx={{ display: 'grid', gap: 1.25 }}>
                  {choices.map((person) => (
                    <Button key={person.uid} variant="outlined" size="large" onClick={() => signInAs(person)}
                      sx={{ justifyContent: 'flex-start', textAlign: 'left', py: 1.5, px: 2, textTransform: 'none' }}>
                      <Box sx={{ display: 'grid', minWidth: 0 }}>
                        <Typography sx={{ fontWeight: 600, fontSize: 15 }}>{person.name}</Typography>
                        {(person.department || person.location) && (
                          <Typography variant="body2" color="text.secondary">
                            {[person.department, person.location].filter(Boolean).join(' · ')}
                          </Typography>
                        )}
                      </Box>
                    </Button>
                  ))}
                </Box>
                <Box>
                  <Link component="button" variant="body2" onClick={() => { setStep('email'); setChoices([]); setError(''); }}>Use a different email</Link>
                </Box>
              </>
            )}

            {step === 'form' && session && (
              <>
                <Fold show={!!error}><Alert severity="error">{error}</Alert></Fold>
                <TypeCards value={type} onPick={setType} closed={closedTypes} />
                {/* Another form cross-fades in, and the block's height follows. */}
                <AutoHeight>
                  <AnimatePresence mode="wait" initial={false}>
                    <m.div key={type} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, transition: EXIT }}>
                      <RequestForm type={type} values={values} onChange={setValue} hourlyStaff={hourlyStaff} carryoverWindow={carryoverWindow} />
                    </m.div>
                  </AnimatePresence>
                </AutoHeight>
                {/* Days and balances after approval, once the form is complete. */}
                <BalancePreview session={session} type={type} body={formReady ? buildRequestBody(type, values) : null} vacationTaken={myBalances?.vacation_taken ?? null} onBookingLimit={setBookingLimit} />
                {/* Send is blocked when the dates run past the booking limit; the reason is shown in red in the preview above (#407). */}
                <Button variant="contained" size="large" sx={big} disabled={!formReady || busy || !!bookingLimit?.refused} onClick={submitAsEmployee}>
                  {/* While the request is being created the label says so, beside the spinner (#455). */}
                  <BusyLabel busy={busy}>{busy ? 'Creating request...' : 'Send request'}</BusyLabel>
                </Button>
              </>
            )}

            {/* A new starter says who they are first (#226), then makes the request. */}
            {step === 'newhire' && verified && (
              <>
                <Heading>Tell us who you are</Heading>
                <Alert severity="info">
                  {email.trim()} is not in the Staff Directory yet. Tell us who you are and who your
                  supervisor is; they will be asked to add you, and your request goes to them once they do.
                </Alert>
                <Fold show={!!error}><Alert severity="error">{error}</Alert></Fold>
                <WhoYouAre verified={verified} value={starter} onChange={setStarter} onError={setError} />
                <Button variant="contained" size="large" sx={big} disabled={!starterReady(starter)}
                  onClick={() => { setError(''); setStep('newhire-request'); }}>
                  Continue to your request
                </Button>
              </>
            )}

            {step === 'newhire-request' && (
              <>
                <Heading>Your request</Heading>
                <Lead>
                  {starter.first.trim()} {starter.last.trim()}, {starter.department}. Supervisor:{' '}
                  {starter.useEmail ? starter.supervisorEmail.trim() : starter.supervisor?.name}.{' '}
                  <Link component="button" variant="body2" onClick={() => { setError(''); setStep('newhire'); }}>Change</Link>
                </Lead>
                <Fold show={!!error}><Alert severity="error">{error}</Alert></Fold>
                <TypeCards value={type} onPick={setType} closed={closedTypes} />
                {/* Another form cross-fades in, and the block's height follows. */}
                <AutoHeight>
                  <AnimatePresence mode="wait" initial={false}>
                    <m.div key={type} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, transition: EXIT }}>
                      <RequestForm type={type} values={values} onChange={setValue} carryoverWindow={carryoverWindow} />
                    </m.div>
                  </AnimatePresence>
                </AutoHeight>
                <Button variant="contained" size="large" sx={big} disabled={!newHireReady || busy} onClick={submitAsNewHire}>
                  {/* While the request is being created the label says so, beside the spinner (#455). */}
                  <BusyLabel busy={busy}>{busy ? 'Creating request...' : 'Send request'}</BusyLabel>
                </Button>
              </>
            )}

            {/* A request waiting to be set up: its status, and a re-pick when handed back (#442, #443). */}
            {step === 'held-status' && (
              heldStatus === 'not_on_team' ? (
                <>
                  <Heading>Pick your supervisor again</Heading>
                  <Lead>
                    {heldWith || 'Your supervisor'} let us know you are not on their team. Choose the right
                    supervisor and we will ask them instead. Nothing about your request changes.
                  </Lead>
                  {heldSummary && (
                    <Typography variant="body2" color="text.secondary">Your request: {heldSummary}</Typography>
                  )}
                  <Fold show={!!error}><Alert severity="error">{error}</Alert></Fold>
                  {verified && heldDept ? (
                    <SupervisorPicker verified={verified} department={heldDept} value={repickChoice} onChange={setRepickChoice} />
                  ) : !verified ? (
                    // Resumed after a reload with no verified token in this tab (#482):
                    // the re-pick needs one, so send them back to sign in with email.
                    <Typography variant="body2" color="text.secondary">
                      <Link component="button" variant="body2" onClick={() => navigate('/', { replace: true })}>
                        Sign in with your email
                      </Link>{' '}to pick another supervisor.
                    </Typography>
                  ) : (
                    <CircularProgress size={20} />        // the department is still loading from the first poll
                  )}
                  <Button variant="contained" size="large" sx={big}
                    disabled={!supervisorReady(repickChoice) || busy || !verified || !heldDept} onClick={submitRepick}>
                    <BusyLabel busy={busy}>Ask this supervisor</BusyLabel>
                  </Button>
                </>
              ) : heldStatus === 'sent' ? (
                <>
                  <Box sx={(t) => ({
                    width: 64, height: 64, borderRadius: '50%', display: 'grid', placeItems: 'center',
                    bgcolor: t.tokens.okSoft, color: 'success.main',
                    animation: `${KF.pop} .5s cubic-bezier(.2,1.6,.4,1) both !important`,
                  })}><DrawnTick size={34} delay={0.3} stroke={3} /></Box>
                  <Heading>Request sent</Heading>
                  <Lead>{doneText || `${heldWith || 'Your supervisor'} confirmed it, so your request is on its way.`}</Lead>
                  <Box>
                    <Button variant="contained" onClick={() => { clearSession(); navigate('/', { replace: true }); }}>
                      Sign in
                    </Button>
                  </Box>
                </>
              ) : heldStatus === 'cancelled' ? (
                <>
                  <Heading>Request cancelled</Heading>
                  <Lead>{doneText || 'This request was cancelled. If you still need it, start again with your email.'}</Lead>
                  <Box>
                    <Button variant="outlined" onClick={() => navigate('/', { replace: true })}>Back to start</Button>
                  </Box>
                </>
              ) : heldStatus === 'refused' ? (
                // The release was refused on a booking rule (a date clash, the
                // booking limit): say it was not sent, show the exact reason, and
                // point them at what to do. They have also been emailed (#406).
                <>
                  <Heading>Request not sent</Heading>
                  <Lead>{doneText || 'Your request could not be sent.'}</Lead>
                  <Typography variant="body2" color="text.secondary">
                    We emailed you about it. Start again with other dates, or cancel or change the request it clashes with.
                  </Typography>
                  <Box>
                    <Button variant="outlined" onClick={() => navigate('/', { replace: true })}>Back to start</Button>
                  </Box>
                </>
              ) : (
                <>
                  <Heading>Your request is saved</Heading>
                  <Lead>
                    We asked {heldWith || 'your supervisor'} to add you to the staff list.
                    Your request goes to them automatically, and you will get an email when it is approved or rejected. You can close this page.
                  </Lead>
                  <NextSteps items={[
                    { title: 'Request saved', note: 'Just now', done: true },
                    { title: `${heldWith || 'Your supervisor'} adds you to the staff list`, note: 'They got an email with a link' },
                    { title: 'Your request goes to them', note: 'Automatically' },
                    { title: 'You get an email with the decision' },
                  ]} />
                </>
              )
            )}

            {step === 'done' && (
              <>
                {/* A tick that pops in, then draws itself; hidden for a refusal,
                    which is not a success (#406). */}
                {heldStatus !== 'refused' && (
                  <Box sx={(t) => ({
                    width: 64, height: 64, borderRadius: '50%', display: 'grid', placeItems: 'center',
                    bgcolor: t.tokens.okSoft, color: 'success.main',
                    animation: `${KF.pop} .5s cubic-bezier(.2,1.6,.4,1) both !important`,
                  })}><DrawnTick size={34} delay={0.3} stroke={3} /></Box>
                )}
                <Heading>
                  {!held || added ? 'Request sent'
                    : heldStatus === 'refused' ? 'Request not sent'
                    : heldStatus === 'cancelled' ? 'Request cancelled'
                    : 'Request saved'}
                </Heading>
                <Lead>{doneText}</Lead>
                {next.length > 0 && <NextSteps items={next} />}
                <Box sx={{ display: 'flex', gap: 1.25, flexWrap: 'wrap', alignItems: 'center' }}>
                  <Button variant="contained" onClick={another}>Make another request</Button>
                  {session && <Button variant="outlined" onClick={() => navigate('/my')}>See my requests</Button>}
                  {/* Released but the automatic sign-in was refused: a plain way in (#482). */}
                  {!session && heldSignedOut && (
                    <Link component="button" variant="body2" onClick={() => navigate('/', { replace: true })}>
                      Sign in with your email to see your requests
                    </Link>
                  )}
                </Box>
              </>
            )}
          </Box>
          </m.div>
          </AnimatePresence>
        </Box>
      </Box>
    </ThemeProvider>
  );
}
