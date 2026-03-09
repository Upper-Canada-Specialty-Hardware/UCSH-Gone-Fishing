# 00 - System Overview

## Purpose

This system manages employee leave requests, overtime/make-up time tracking, and carry-over/payout balance transfers for UCSH. All data resides in SharePoint lists. The server will use Microsoft Graph API for SharePoint access, Office 365 email via Graph API, and Twilio for SMS.

> **Note:** Reporting flows (EOM Manager Report, Raw Data Manager Init Report, Monthly SD Export) are excluded from this migration and will be reimagined separately.

---

## SharePoint Lists

All lists are on site: `https://ucshca.sharepoint.com/sites/UCSHBulletinBoard`

| List | GUID | Purpose |
|------|------|---------|
| Staff Directory | `ed4bba96-f035-4eee-a8ff-af71036034fe` | Central employee records with balances, manager assignments, metadata |
| Leave Requests | `bd21037a-9c3c-4682-9aaa-948095e16aec` | All leave request records |
| Overtime Requests | `1ea6f753-5c84-450f-b266-707d73c71133` | All overtime / make-up time requests |
| CarryOver Payout | `bcfd8cc6-b29f-4d6f-a449-cb1d0a9251bb` | Balance transfer requests (carry over or payout) |
| Company Holidays | `391e299d-c537-44c4-90c8-462e2ca2db5f` | Province-specific holidays and half-friday season markers |

---

## Location-to-Province Mapping

Employee locations (from Staff Directory `Location` field) map to provinces for holiday filtering:

| Location | Province Code |
|----------|--------------|
| Toronto Victoria Park | ON |
| Toronto Warden | ON |
| Ottawa | ON |
| Leaside | ON |
| Barrie | ON |
| British Columbia | BC |
| Newfound Land | NL |

If no location matches, the system should fail with error "Province cannot be determined."

---

## Balance Types

The Staff Directory tracks five balance types per employee:

| Balance | SD Column | Unit | Description |
|---------|-----------|------|-------------|
| Vacation | CurrentVacationBalance | Days | Current vacation balance. Replenished annually (Vacation Entitlement). |
| Sick / Personal | CurrentSickDayBalance | Days | Current sick day balance. Replenished annually (Sick Day Entitlement). |
| Carry Over | CarryOver | Days | Days carried over from a previous year's vacation. |
| Make-Up (Overtime) | CurrentOvertimeBalance | Days | Accumulated overtime credit in days (hours / 8). |
| Payout | Payout | Days | Accumulated payout days. Capped at 5 per calendar year. |

### Balance Cascading Rules

When a leave request is approved, the system deducts days from a starting balance. If that balance goes negative, the negative amount cascades to the next balance in sequence. This continues until all negatives are resolved.

**Current Year Requests — Sequence: Sick → Overtime → CarryOver → Vacation**

The initial deduction depends on leave type:
- **Vacation / Half Day / Partial Day Off:** Deduct from `CurrentOvertimeBalance` (Make-Up) first
- **Sick / Personal Day:** Deduct from `CurrentSickDayBalance` first
- **Bereavement / Jury Duty:** No balance adjustment at all

After the initial deduction, cascade negative balances:

1. If `CurrentSickDayBalance < 0`: Transfer remainder to Overtime → `CurrentOvertimeBalance = CurrentOvertimeBalance + CurrentSickDayBalance`, set `CurrentSickDayBalance = 0`
2. If `CurrentOvertimeBalance < 0`: Transfer remainder to CarryOver → `CarryOver = CarryOver + CurrentOvertimeBalance`, set `CurrentOvertimeBalance = 0`
3. If `CarryOver < 0`: Transfer remainder to Vacation → `CurrentVacationBalance = CurrentVacationBalance + CarryOver`, set `CarryOver = 0`
4. Once no negative balances remain → cascading is complete

**Next Year Requests — Sequence: Overtime → CarryOver**

For requests where the start or end date falls in the next calendar year:

1. If `CurrentOvertimeBalance < 0`: Transfer remainder to CarryOver → `CarryOver = CarryOver + CurrentOvertimeBalance`, set `CurrentOvertimeBalance = 0`
2. Once balanced → done (Vacation is not touched for next-year requests)

**Implementation Detail:** The cascading runs as a loop (up to 60 iterations). Each iteration re-reads the employee's current balances from SharePoint, checks for negatives, and adjusts. The loop exits when all checked balances are non-negative.

---

## SystemCheck Locking Mechanism

The `SystemCheck` field on Staff Directory acts as a simple mutex:

- **Ready:** No flow is currently modifying this employee's balances. Safe to proceed.
- **Editing:** A flow is actively updating balances. Other flows must wait.

### Usage Pattern

1. Before modifying balances, a flow polls `SystemCheck` in a loop (up to 60 iterations, 30-second delay between checks)
2. Once `SystemCheck == "Ready"`, proceed
3. Set `SystemCheck = "Editing"` before updating any balance fields
4. After all balance updates are complete, set `SystemCheck = "Ready"`

This prevents race conditions when multiple approvals happen simultaneously for the same employee.

---

## Notification Channels

### Email (Office 365 via Graph API)

- Used for all approval requests, approval/rejection confirmations, balance update notifications, and alert emails
- Sender address: `HR@UCSHCA.onmicrosoft.com`
- Admin recipients: `mandyl@ucsh.com`, `jayp@ucsh.com`, `davep@ucsh.com`, `generalmail@ucsh.com` (varies by flow)

### SMS (Twilio)

- Used as an alternative approval channel for leave requests only
- Twilio phone number: `+16476977133`
- Managers can approve/reject by replying to SMS
- SMS webhook endpoint receives the Twilio POST, parses the Body and From fields

---

## Half-Day Fridays

A seasonal policy where Fridays are half-days (employees work 4 hours instead of 8).

- Defined by two special entries in Company Holidays list: `Half Fridays START` and `Half Fridays END` with their respective dates
- Province-specific (each province has its own start/end dates)
- When an employee takes leave on a Friday within the half-friday season, 0.5 days is deducted from the calculated business days
- For partial day off requests on a half-day Friday, the maximum allowed hours is 4 (>4 hours is auto-rejected)
- For overtime requests on a half-day Friday, the approval email subject includes "Half-Day Friday Detected"

---

## Hourly Staff Exception

Employees with `SalaryHourly == "Hourly"` in the Staff Directory:
- Their leave and overtime requests are processed (approved/rejected) normally
- Approval/rejection emails are sent
- **No balance adjustments are made** — the flow terminates before the balance deduction/cascading step
- A different email template is used (simpler, without balance information)

---

## Shared Concepts

### Next Year Detection

A request is considered "next year" if:
- `year(StartDate) == currentYear + 1`, OR
- `year(EndDate) == currentYear + 1`

Next-year requests use a different balance cascading sequence (Overtime → CarryOver only, skipping Vacation).

### Business Day Calculation

Leave days are calculated by iterating from Start Date to End Date (inclusive):
1. Skip weekends (Saturday = 6, Sunday = 0)
2. Count each weekday as 1 day
3. If the day is a Friday within the half-friday season, subtract 0.5 from the count
4. If the day matches a company holiday (for the employee's province), subtract 1 from the count
5. Holidays named "Half Fridays START" or "Half Fridays END" are excluded from holiday deduction (they are markers, not actual holidays)

### Request Allow Date

A calculated date per employee that controls how far into the future they can submit leave requests. Recalculated inline after any balance change (leave approval, overtime approval, carryover/payout approval) for the affected employee only. Logic is based on vacation and carry-over balances — see `05-MAINTENANCE.md` for details.

---

## Flow Inventory

| Flow | Source Trigger | Description |
|------|---------------|-------------|
| 01 New Leave Request | MS Forms webhook | Creates Leave Request list item from form submission |
| 02 Leave Request Auto Calculate Days | SP list item created | Calculates business days, handles auto-rejections |
| 03 Leave Request Auto Manager | SP list item created | Assigns manager, location, department from SD |
| 04 Leave Request Approval | SP list item modified | Email approval workflow with balance adjustments |
| 07 Leave Request SMS Approval | HTTP webhook (Twilio) | SMS approval workflow with balance adjustments |
| 08 Leave Request Bereavement/Jury Duty Alert | SP list item created | Alert emails for bereavement/jury duty leaves |
| 01 New Overtime Request | MS Forms webhook | Creates Overtime Request list item from form |
| 02 Overtime Request Auto Manager | SP list item created | Assigns manager from SD |
| 03 Overtime Request Approval | SP list item modified | Email approval with balance updates and vacation offset |
| 01 New CarryOver Payout Request | MS Forms webhook | Creates CarryOver/Payout list item from form |
| 02 CarryOver Payout Auto Manager | SP list item created | Assigns manager, employee/manager IDs |
| 03 CarryOver Payout Approval | SP list item modified | Email approval with balance transfer and validation |
| Request Allow Date Recalculation | Inline (after any balance change) | Updates Request Allow Date for the affected employee |

**Excluded from migration (to be reimagined later):**
- 10 LR Raw Data Manager Init Report
- EOM Manager Report
- 99 Monthly SD Export
