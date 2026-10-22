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
    fix: 'Confirm this person has a staff record.',
  },
  identity_roundtrip: {
    title: 'Email not linked to their record',
    fix: 'Make sure the email on their staff record is the one they sign in with, so the system can tell who submitted a request.',
  },
  supervisor_set: {
    title: 'No supervisor assigned',
    fix: 'Set their supervisor in their profile so their requests have someone to approve them.',
  },
  supervisor_resolves: {
    title: 'A supervisor does not match a real employee',
    fix: 'Re-pick their supervisor from the directory so approvals reach the right person.',
  },
  location_province: {
    title: 'Office location not recognized',
    fix: 'Choose a valid office location so vacation and leave days calculate correctly.',
  },
  identity_name_spacing: {
    title: 'Stray spaces in the name or email',
    fix: 'Re-save their name and email in the person panel so the extra spaces (and any capitals in the email) are tidied. Requests are matched by an exact name and email, so while they are there a request can route to the wrong person or to nobody.',
  },
  identity_unique_name: {
    title: 'Someone else has the same name',
    fix: 'Two staff records share this name. Requests are matched back to a person by name, so one of the records needs a distinguishing name before their requests can be routed reliably.',
  },
  manager_m365_match: {
    title: 'A supervisor’s email is not recognised',
    fix: 'Correct the supervisor’s email in their profile. Until it does, their requests cannot record a manager - nobody is asked to approve, and the request stays hidden from the dashboards.',
  },
  employment_type_fits: {
    title: 'Employment type looks wrong',
    fix: 'The record says hourly but carries salaried vacation values. If they are salaried, change their employment type; if they are hourly, clear the vacation grant and balance in the person panel.',
  },
  approved_balance_unmoved: {
    title: 'An approved request never moved a balance',
    fix: 'Use Settle requests to go through each one: take the days off now, record that they were already taken off by hand, or mark that no balance was needed.',
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
  pending_over_booking_limit: {
    title: 'A pending vacation runs past the booking limit',
    fix: 'The dates on this request are later than this person can book vacation for, so it will be refused at approval. Ask them to pick earlier dates, or reject it.',
  },
};
