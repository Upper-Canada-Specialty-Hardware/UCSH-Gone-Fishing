// Plain-language title + fix for each setup problem code, written for a
// non-technical HR admin. The backend's `detail` (which carries the specifics)
// is shown under the title; this is the human "what it means / what to do".
//
// Shared by the two views onto the same checks - the whole-directory list
// (EmployeeSetupList) and the per-employee report (EmployeeValidation) - so a
// problem is worded the same wherever it is named. Codes come from
// backend/app/services/employee_validation.py; keep them in step.
export const PROBLEM_INFO: Record<string, { title: string; fix: string }> = {
  employee_record: {
    title: 'Employee record not found',
    fix: 'Confirm this person has a row in the Staff Directory.',
  },
  identity_roundtrip: {
    title: 'Not linked to Microsoft 365',
    fix: 'Make sure the email on the Staff Directory record matches their Microsoft 365 account, so the system can tell who submitted a request.',
  },
  supervisor_set: {
    title: 'No supervisor assigned',
    fix: 'Set their supervisor in the Staff Directory so their requests have someone to approve them.',
  },
  supervisor_resolves: {
    title: 'A supervisor does not match a real employee',
    fix: 'Re-pick their supervisor from the directory so approvals reach the right person.',
  },
  manager_reachable: {
    title: 'A supervisor has no email address',
    fix: 'Add an email address for the supervisor so approval emails can reach them.',
  },
  location_province: {
    title: 'Office location not recognized',
    fix: 'Choose a valid office location so vacation and leave days calculate correctly.',
  },
  holidays_load: {
    title: 'No holiday calendar for their province',
    fix: 'Add holidays for their province, otherwise every weekday counts as a workday.',
  },
  balances_numeric: {
    title: 'A balance value is not a number',
    fix: 'Correct the balance value on their Staff Directory record.',
  },
  identity_unique_name: {
    title: 'Someone else has the same name',
    fix: 'Two staff records share this name. Requests are matched back to a person by name, so one of the records needs a distinguishing name before their requests can be routed reliably.',
  },
  manager_m365_match: {
    title: 'A supervisor’s email does not match a Microsoft 365 account',
    fix: 'Correct the supervisor’s email so it matches their Microsoft 365 account. Until it does, their requests cannot record a manager - nobody is asked to approve, and the request stays hidden from the dashboards.',
  },
  supervisor_not_self: {
    title: 'They are listed as their own supervisor',
    fix: 'Change their supervisor in the Staff Directory, otherwise their requests are sent to them to approve.',
  },
  holidays_current_year: {
    title: 'No holidays for the current year',
    fix: 'Add this year’s holidays for their province. Without them, holidays are counted as ordinary workdays and leave is over-deducted.',
  },
  balances_in_range: {
    title: 'A balance value looks wrong',
    fix: 'Check the balance against their history - a value outside the expected range usually means an approval only half-applied.',
  },
  entitlements_set: {
    title: 'A yearly entitlement is missing',
    fix: 'Set their yearly vacation and sick entitlements in the Staff Directory so their annual grant can be worked out.',
  },
  requests_missing_days: {
    title: 'A request never got its days worked out',
    fix: 'Reprocess the request from Stuck Requests. Until then it is hidden from every dashboard and nobody can action it.',
  },
  requests_missing_manager: {
    title: 'A request never got a manager',
    fix: 'Reprocess the request from Stuck Requests. Nobody was asked to approve it, and it does not appear on any dashboard.',
  },
  requests_not_notified: {
    title: 'A request was never sent to its manager',
    fix: 'The request has a manager but the approval was never sent. Reprocess it from Stuck Requests to send it.',
  },
  requests_auto_rejected: {
    title: 'The system recently rejected a request on its own',
    fix: 'Read the reason shown above. If it is wrong, the employee can submit again once the cause is cleared.',
  },
  requests_approved_dates: {
    title: 'Dates already booked',
    fix: 'These approved requests hold their dates. A new request covering the same days will be refused at approval.',
  },
};
