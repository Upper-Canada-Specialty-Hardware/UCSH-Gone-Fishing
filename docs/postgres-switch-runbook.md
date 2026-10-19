# Postgres switch runbook (#185 requests, #261 staff)

This covers the rehearsal on the Railway "pr preview" environment and, at the end, what changes for the production switch. Until the switch, SharePoint stays the system of record and Postgres is a shadow copy that is refreshed every 5 minutes.

## Rehearsal environment

| | |
|---|---|
| Frontend | https://frontend-pr-preview-89e6.up.railway.app |
| Backend | https://backend-gone-fishing-pr-preview.up.railway.app |
| Branch | `feat/261-staff-postgres` (both services) |
| Database | the preview's own Postgres-BzHD (never production's) |

Rehearsal mode is on (`REHEARSAL_MODE=true`):
- The app reads the live SharePoint lists but never writes to them.
- No Graph webhooks are registered.
- Emails and texts go only to `REHEARSAL_ALLOWLIST` (roberto@ucsh.com). Everything else is logged and dropped.

`PROCESSING_ENABLED=true`, so requests, approvals and the background jobs run in full inside the preview.

## Before the switch: checks

1. **Deployments.** Both preview deployments show SUCCESS. The backend log shows the database revisions upgrading to `0022` and then "Shadow sync" lines every 5 minutes.
2. **Admin login.** Open the admin dashboard on the preview frontend. The preview has its own admin password: the seeded starting one unless it was changed there.
3. **Storage card.** Admin dashboard, storage card:
   - requests, staff and holidays all read "SharePoint";
   - the last sync time is recent;
   - the request and staff counts are close to production's.
4. **Legacy data screen.** Review the findings from the import. These are flags for HR and change nothing.
5. **Allowlist phone.** Once your staff record has imported, your cell number gets added to `REHEARSAL_ALLOWLIST` (a variable change, which redeploys the backend) so result texts reach you.
6. **Your manager.** In the preview only, set yourself as your own manager (Staff tab, open your profile, Managers). The approval email then reaches an allowlisted address, so the email-link approval can be tested. This change stays in the preview database because rehearsal mode never writes to SharePoint.

## The switch (only when Robert says so)

Admin dashboard, storage card, **Switch to Postgres**, then confirm in the panel. What happens:
1. New requests, approvals, rejections, refunds and staff edits are paused. Anyone who tries gets "The system is being updated. Please try again in a minute."
2. The switch waits for anything already in flight to finish (up to 2 minutes; if something is still running then, the switch fails and nothing changes).
3. The final sync copies SharePoint's latest requests, staff and company holidays into Postgres, and each request's SharePoint version history: every field change it went through (its history tab), and, for an old approval with no balance record, the balance writes it made, found in the person's Staff Directory history. If any part of it fails, the switch stops and nothing changes.
4. The Graph webhook subscriptions are deleted (in rehearsal only the database rows go; no live subscription is touched).
5. In one transaction: every staff manager and supervisor link is moved from its Microsoft 365 user id to the staff id of the person it names, and requests, staff and holidays all switch to Postgres.
6. The pause ends. If anything fails, the pause also expires by itself after 15 minutes.

The switch sends no email or text and reprocesses no request. The final sync only copies; nothing in it queues a message.

### Confirming it in the backend log

Every line the switch writes starts with `Switch:`. A clean run reads, in order:
- `Switch: started`, then `Switch: step waiting_in_flight`, `Switch: step final_sync`;
- one `Switch: final sync <step>: {...}` line per copy step (staff, requests, history, holidays), each with its counts and `errors: 0`. The history line counts `requests_read`, `field_changes`, `matched`, one `not_matched_<reason>` per reason, and `remaining: 0`;
- `Switch: step switching`, `Switch: removed N SharePoint webhook subscription(s)`;
- `Switch: manager links moved to staff ids: {'links': N, 'unmatched': N}` (unmatched = managers who are not on staff);
- `Switch: Postgres now holds {...}`: requests per kind, pending per kind, staff, holidays, manager links, and `messages_queued_by_switch: 0`;
- `Switch: complete`.

A failure logs `Switch: FAILED, nothing was switched: <reason>` and leaves every setting on SharePoint. One expected failure: the version history is copied a slice at a time by the shadow sync (about 4 minutes of every 5-minute run), and the first full copy takes about 50 minutes. Switching before it has caught up fails with "SharePoint history is still being copied: N requests left" once the final sync has spent 10 minutes on it; wait and try again. After a good run there must be no `SharePoint call refused` line.

The switch runs in the background; the storage card shows its progress and, if it fails, the reason and a "Try again" button.

A second switch is refused ("Already switched"). There is no switch-back button; resetting the rehearsal means resetting the preview database's settings.

After the switch the app never talks to SharePoint again. Postgres is the only source of truth, with no fallback either way:
- every Graph call is refused at one gate and logged as "SharePoint call refused after the switch";
- on restart the app skips the Graph token, the SharePoint probe, webhooks, catch-up and the 5 minute sync; the reminder, carry-over reset and new hire jobs run on Postgres alone (the setup-nudge emails are gone; broken staff records show on the admin setup tab);
- incoming webhook notifications are acknowledged and ignored;
- nothing checks SharePoint after the switch, and staff edits are not copied back to the Staff Directory (the code for both was removed);
- the holiday copy panel and the request columns card are gone from the admin dashboard.

## Tests after the switch (roberto@ucsh.com)

For each test, watch three things:
- an open dashboard tab updates without a reload;
- the request's timeline shows each stage;
- one result email arrives, showing the balance lines.

1. **Hourly pipeline** (your record is Hourly). Submit a vacation request and a sick request on the request page and approve each one.
   - Vacation: the vacation counter goes up and no balance is spent.
   - Sick: the sick balance goes down in whole days.
   - Refund one and check that it reverses.
2. **Switch your record to Salary**: in the admin dashboard, Staff tab, open your profile, then Placement, then "Salary or hourly". The save asks for a reason.
3. **Salaried pipeline.** Submit a vacation request, an overtime request and, if the window is open, a carry-over or payout. Approve each one.
   - Vacation spends Make-Up first, then the next pot in the chain.
   - Overtime adds to Make-Up.
   - Carry-over moves days out of vacation.
   - Then reject one, refund one, and approve one through the email link.
4. **Switch back to Hourly** and check that requests made earlier keep the rules they were approved under.
5. **New employee pipeline.** Add an employee from the admin dashboard (and from the manager dashboard). They get an app id of 1,000,000 or above, appear in the dashboards at once, and can submit a request. Nothing is copied to SharePoint.
6. **Holidays.** Add, edit and delete a company holiday in the admin dashboard; a leave request over that date counts one day less.
7. **No SharePoint traffic.** Watch the backend log through all of the above: there must be no "SharePoint call refused" line. One would mean a code path still reaching for SharePoint.

## The Microsoft Form and Power Automate during the rehearsal

They stay on. They write only into the SharePoint lists; nothing in the app is called by them.
- Before the preview switch, the 5 minute sync copies what they write into the preview's Postgres (read only; rehearsal never processes or emails it beyond the allowlist).
- After the preview switch, the preview never reads SharePoint, so they cannot touch the preview's Postgres. From then on the preview and production drift apart: Form requests and production decisions made after that moment do not appear in the preview. That is expected for a rehearsal.
- Production keeps running on SharePoint as today, Form included.

## Production switch: what differs

- Production runs without `REHEARSAL_MODE`, so messages go to everyone and the switch deletes the live Graph webhook subscriptions.
- The Microsoft Form and its Power Automate flow stay on (product owner ruling, 2026-10-18). After the switch the app never reads SharePoint, so anything submitted through the Form, or typed into a SharePoint list, is not taken in. That is accepted: requests are made on the request page, and a Form submission after the switch is a user mistake, not a gap to cover. The flow never writes the Staff Directory.
- Run the switch after hours. The pause lasts as long as the final sync, usually a few minutes.
- Merging to `master` deploys production. The switch itself is a separate, deliberate click afterwards.
