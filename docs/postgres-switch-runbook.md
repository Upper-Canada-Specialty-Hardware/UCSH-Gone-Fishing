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

1. **Deployments.** Both preview deployments show SUCCESS. The backend log shows the database revisions upgrading to `0021` and then "Shadow sync" lines every 5 minutes.
2. **Admin login.** Open the admin dashboard on the preview frontend. The preview has its own admin password: the seeded starting one unless it was changed there.
3. **Storage card.** Admin dashboard, storage card:
   - requests and staff both read "SharePoint";
   - the last sync time is recent;
   - the request and staff counts are close to production's.
4. **Legacy data screen.** Review the findings from the import. These are flags for HR and change nothing.
5. **Allowlist phone.** Once your staff record has imported, your cell number gets added to `REHEARSAL_ALLOWLIST` (a variable change, which redeploys the backend) so result texts reach you.
6. **Your manager.** In the preview only, set yourself as your own manager (Staff tab, open your profile, Managers). The approval email then reaches an allowlisted address, so the email-link approval can be tested. This change stays in the preview database because rehearsal mode never writes to SharePoint.

## The switch (only when Robert says so)

Admin dashboard, storage card, **Switch to Postgres**, then confirm in the panel. What happens:
1. New requests, approvals, rejections, refunds and staff edits are paused. Anyone who tries gets "The system is being updated. Please try again in a minute."
2. A 15 second wait lets anything already in flight finish.
3. The final sync copies SharePoint's latest state into Postgres.
4. Requests and staff both switch to Postgres.
5. The pause ends. If anything fails, the pause also expires by itself after 15 minutes.

A second switch is refused ("Already switched"). There is no switch-back button; resetting the rehearsal means resetting the preview database's settings.

After the switch, the app checks SharePoint 30 minutes later, 1 hour later and then daily:
- edits and deletes made in SharePoint since the switch become findings;
- new request items in SharePoint become findings and are never imported;
- staff differences in SharePoint become findings and are never pulled in.

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
5. **New employee pipeline.** Add an employee from the admin dashboard (and from the manager dashboard). They get an app id of 1,000,000 or above, appear in the dashboards at once, and can submit a request. The copy to SharePoint is dropped in rehearsal; check the log line.
6. **SharePoint checks.** After 30 minutes and after 1 hour, the backend log shows the check runs. The Legacy data screen shows findings only for things that really changed in SharePoint since the switch.

## Production switch: what differs

- Production runs without `REHEARSAL_MODE`, so app staff changes are written back to the SharePoint Staff Directory and messages go to everyone.
- At the switch, turn off intake so nothing new reaches the SharePoint lists:
  - In Microsoft Forms, open each leave, overtime and carry-over form, go to Settings and turn off "Accept responses".
  - In Power Automate, turn off the flow or flows that copy a Form response into a SharePoint request list.
  - This needs an M365 account that owns or co-owns those forms, plus a Power Platform maker or admin for the flow's environment.
  - Missing this step is safe but noisy: anything that still lands in a list is flagged on the Legacy data screen, not imported.
- Run the switch after hours. The pause lasts as long as the final sync, usually a few minutes.
- Merging to `master` deploys production. The switch itself is a separate, deliberate click afterwards.
