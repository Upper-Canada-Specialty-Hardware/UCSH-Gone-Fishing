# 01 - Leave Requests

Complete lifecycle of a leave request from submission through approval/rejection and balance adjustment.

---

## 1. Submission (Flow: 01 New Leave Request)

**Trigger:** MS Forms webhook — when a new response is submitted to the Leave Request form.

### Form Fields Captured

| Form Field | Maps To |
|------------|---------|
| Employee Name | Title (for partial day) |
| First Name + Last Name | Title as `{FirstName LastName} /// {Notes}` (for standard requests) |
| Leave Type | LeaveType choice value |
| Start Date | StartDate (standard requests) |
| End Date | EndDate (standard requests) |
| Partial Day Off Date | StartDate AND EndDate (partial day — same date for both) |
| Partial Day Hours | Used to calculate Days (hours / 8) |
| Responder email | SubmittedTest (via Claims) |

### Submission Logic

1. Get form response details
2. Check if `LeaveType == "Half Day or Partial Day Off"`

**If Partial Day:**
- Initialize a float variable `PartialDayOff` from the hours field
- Compute `Days = PartialDayOff / 8`
- Create SharePoint list item in Leave Requests with:
  - `StartDate` = partial day date
  - `EndDate` = partial day date (same)
  - `Title` = employee name from form
  - `LeaveType` = "Half Day or Partial Day Off"
  - `Status` = "Pending"
  - `Days` = computed value (hours / 8)
  - `SubmittedTest` = responder email (Claims)
  - `ApproveProcessedFlag` = "Not Processed"

**If Any Other Leave Type:**
- Create SharePoint list item with:
  - `StartDate` = start date from form
  - `EndDate` = end date from form
  - `Title` = `{FirstName LastName} /// {Notes}`
  - `LeaveType` = selected leave type
  - `Status` = "Pending"
  - `SubmittedTest` = responder email (Claims)
  - `ApproveProcessedFlag` = "Not Processed"
  - `Days` is NOT set here (calculated by flow 02)

---

## 2. Auto-Calculate Business Days (Flow: 02 Leave Request Auto Calculate Days)

**Trigger:** When an item is created in the Leave Requests list (polled every 1 minute).

### Pre-Processing Steps

1. **Get Employee Location:** Look up submitter's name in Staff Directory → get `Location` value
2. **Map Location to Province:**
   - Toronto Victoria Park, Toronto Warden, Ottawa, Leaside, Barrie → `ON`
   - British Columbia → `BC`
   - Newfound Land → `NL`
   - No match → Terminate with failure "Province cannot be determined"
3. **Get Company Holidays:** Filter Company Holidays list by province
4. **Get Half-Friday Season:** Query holidays list for entries named "Half Fridays START" and "Half Fridays END" (filtered by province), extract their dates

### Partial Day Auto-Rejection Checks

If `LeaveType == "Half Day or Partial Day Off"`, perform these checks BEFORE the day-counting loop:

**Holiday Conflict Check:**
- For each company holiday (excluding entries with "START" or "END" in the title):
  - If `StartDate == holiday.Date`: AUTO-REJECT
    - Update leave request: `Status = "Rejected"`, `ApproveProcessedFlag = "Processed"`
    - Send email to submitter:
      - **To:** Submitter's email
      - **Subject:** "Partial Day Request - Auto Rejected"
      - **Body:** "Your Partial Day-Off has been auto-rejected by the system due to the requested date being a company holiday; {HolidayName}"
    - Include request details (leave type, start date, title)

**Half-Day Friday Hour Limit Check:**
- If ALL of these are true:
  - Day of week of StartDate is Friday (5)
  - StartDate >= Half Fridays START date
  - StartDate <= Half Fridays END date
  - Days > 0.5 (i.e., more than 4 hours)
- Then AUTO-REJECT:
  - Update leave request: `Status = "Rejected"`, `ApproveProcessedFlag = "Processed"`
  - Send email to submitter:
    - **Subject:** "Leave Request {ID} {SubmitterName}"
    - **Body:** "Your Partial Day-Off has been auto-rejected, system has detected the requested date is a Half-Day Friday, and the requested amount of hours exceeds 4 hours."
  - Terminate flow

- If partial day passes all checks → Terminate with "Cancelled" (the Days value was already set during submission; no further calculation needed)

### Business Day Calculation (Non-Partial Days)

Iterate from `startDate` to `endDate` (inclusive), using a Do-Until loop (exits when `startDate > endDate`, max 60 iterations):

For each day:

1. **Weekend Check:** If `dayOfWeek(startDate) == 0` (Sunday) or `dayOfWeek(startDate) == 6` (Saturday):
   - Set `isWeekend = true`
   - Skip to next day (do not increment dayCount)

2. **If Weekday:** Increment `dayCount` by 1

3. **Half-Friday Check:** If ALL of:
   - `dayOfWeek(startDate) == 5` (Friday)
   - `startDate >= specialStart` (Half Fridays START)
   - `startDate <= specialEnd` (Half Fridays END)
   - Then: `dayCount = dayCount - 0.5`

4. **Holiday Check:** For each company holiday:
   - If `startDate == holiday.Date` AND holiday title is NOT "Half Fridays START" AND NOT "Half Fridays END":
     - `dayCount = dayCount - 1`

5. **Advance:** `startDate = startDate + 1 day`

### Update Leave Request

After the loop completes, update the leave request item:
- `Days` = calculated `dayCount`
- `Status` = "Pending" (re-confirmed)
- `StartDate` and `EndDate` preserved

---

## 3. Auto-Assign Manager (Flow: 03 Leave Request Auto Manager)

**Trigger:** When an item is created in the Leave Requests list (polled every 1 minute).

### Logic

1. Look up submitter's name in Staff Directory: `filter Title eq '{SubmittedTest.DisplayName}'`
2. If no match found → do nothing (flow ends)
3. Extract the `Supervisor` field (plain text manager name)
4. Look up manager in Staff Directory: `filter Title eq '{Supervisor}'`
5. Extract the employee's full SD record (for Location and Department)
6. Build the `AllManagers` array from the employee's `AllManagers` multi-person field (Select → Claims)
7. Update the leave request with:
   - `Manager` = manager's email address (Claims)
   - `Managertxt` = manager's display name
   - `AllManagers` = management chain array
   - `StaffLocation` = employee's Location value
   - `StaffDepartment` = employee's Department value

---

## 4. Email Approval (Flow: 04 Leave Request Approval)

**Trigger:** When an item is modified in the Leave Requests list (polled every 1 minute).

### Pre-Checks

The flow triggers on ANY modification. It must filter to only process valid approval scenarios:

1. **Manager Must Be Assigned:** If `Manager == null` → terminate (auto-manager flow hasn't run yet)
2. **Status Must Be Pending:** If `Status != "Pending"` → terminate
3. **Must Not Be Already Processed:** If `ApproveProcessedFlag == "Processed"` → terminate

### Lookup Employee and Manager

1. Get employee from SD: `filter Title eq '{SubmittedTest.DisplayName}'`
2. Get manager from SD: look up by the manager's name from the leave request
3. Extract employee ID and manager ID for subsequent lookups

### SystemCheck Wait Loop

Before accessing balances, wait for the employee's `SystemCheck` to be "Ready":
- Poll the employee's SD record in a Do-Until loop (max 60 iterations)
- If `SystemCheck != "Editing"` → proceed (set variable to "Ready")
- If `SystemCheck == "Editing"` → wait 30 seconds and retry

### Send Approval Email

Send email with options (Approve / Reject) to the manager:
- **To:** Manager's email address
- **Subject:** "Leave Request - {SubmitterName}"
- **Options:** "Approve, Reject"
- **Body:** Includes leave type, dates, days, description, current balances

### Process Response

**If Approved:**

1. Update leave request:
   - `Status` = "Approved"
   - `ApproveProcessedFlag` = "Processed"
   - `ApprovedDate` = current date (`yyyy-MM-dd`)

2. Send confirmation email to employee:
   - **To:** Employee's email
   - **Subject:** "{SubmitterName} - Leave Request: Approved"
   - **Body:** Approved by, leave details, leave type, period

3. Send SMS confirmation to manager

4. **Hourly Staff Check:** If employee's `SalaryHourly == "Hourly"` → terminate (no balance adjustment)

5. **Balance Deduction** — varies by leave type:
   - **Vacation or Half Day / Partial Day Off:** Deduct `Days` from `CurrentOvertimeBalance` (Make-Up)
   - **Sick or Personal Day:** Deduct `Days` from `CurrentSickDayBalance`
   - **Bereavement or Jury Duty:** No balance adjustment → terminate

6. Set `SystemCheck = "Editing"` on the employee's SD record

7. **Determine Cascading Sequence:**
   - Check if request is for **next year** (start or end date year == current year + 1)
   - If next year → use Next Year sequence
   - If current year → use Current Year sequence

8. **Run Balance Cascading Loop** (see below)

9. After cascading completes:
   - Recalculate `Request Allow Date` for this employee (see `05-MAINTENANCE.md`)
   - Send balance update email to employee, manager, and actual manager (if different)
   - Set `SystemCheck = "Ready"` on employee's SD record
   - Log new balances to the leave request's `NewBalances` field

**If Rejected:**

1. Update leave request:
   - `Status` = "Rejected"
   - `ApproveProcessedFlag` = "Processed"

2. Send rejection email to employee:
   - **To:** Employee's email
   - **Subject:** "{SubmitterName} - Leave Request: Rejected"
   - **Body:** Rejected by, leave details, leave type, period

### Balance Cascading — Current Year

Runs as a Do-Until loop (max 60 iterations, exits when `balance == true`):

Each iteration:
1. Re-read the employee's current balances from SD
2. Check `CurrentSickDayBalance < 0`:
   - If yes: `CurrentOvertimeBalance = CurrentOvertimeBalance + CurrentSickDayBalance`, `CurrentSickDayBalance = 0` → update SD → set `balance = false` (continue loop)
3. Check `CurrentOvertimeBalance < 0`:
   - If yes: `CarryOver = CarryOver + CurrentOvertimeBalance`, `CurrentOvertimeBalance = 0` → update SD → set `balance = false`
4. Check `CarryOver < 0`:
   - If yes: `CurrentVacationBalance = CurrentVacationBalance + CarryOver`, `CarryOver = 0` → update SD → set `balance = false`
5. If none are negative: set `balance = true` (exit loop)

### Balance Cascading — Next Year

Same loop structure, but only checks:
1. `CurrentOvertimeBalance < 0`:
   - If yes: `CarryOver = CarryOver + CurrentOvertimeBalance`, `CurrentOvertimeBalance = 0` → set `balance = false`
2. If not negative: set `balance = true` (exit loop)

### Balance Update Email (Current Year)

- **To:** Employee email; Manager email; Actual manager email (if different)
- **Subject:** "Updated Leave Balance - {EmployeeName}"
- **Body:**
  ```
  Updated Balances for ({EmployeeName}):

  Vacation: {CurrentVacationBalance}
  Sick/Personal: {CurrentSickDayBalance}
  Carry Over: {CarryOver}
  Overtime: {CurrentOvertimeBalance}
  ```

### Balance Update Email (Next Year)

Same as above, with additional note: "The approved request is dated for the next calendar year. The request's deductions were applied to your Overtime and Carry Over balances."

### New Balances Logged to Leave Request

Format: `(Vacation:{val})(Sick:{val})(CarryOver:{val})(Make-Up:{val})`

---

## 5. SMS Approval (Flow: 07 Leave Request SMS Approval)

**Trigger:** HTTP webhook (receives POST from Twilio when an SMS is received at `+16476977133`).

### SMS Parsing

The incoming request body is a URL-encoded string. The flow:

1. Splits the body by `&` to get key-value pairs
2. For each pair:
   - If contains `Body=`: Extract the value after `=`
     - If contains "Approve" or "approve" → set `decision = "Approve"`
     - If contains "Reject" or "reject" or "yeet" or "YEET" → set `decision = "Reject"`
     - Extract the item ID: split the body value by `+`, take the second element, convert to integer
   - If contains `From=`: Extract the last 10 characters as the manager's cell number

### Validation Steps

1. **Get Leave Request:** Look up the leave request by the extracted item ID
   - If the lookup fails (request doesn't exist): Send SMS reply "Request #{itemid} does not exist, please try again." → terminate

2. **Look Up Manager by Cell:** Query SD where `CellNumber eq '{from}'`
3. **Look Up Employee:** Query SD where `Title eq '{leave request SubmittedTest.DisplayName}'`

4. **Already Processed Check:** If `ApproveProcessedFlag == "Processed"` OR `Status != "Pending"`:
   - Send SMS: "Request #{itemid} has already been processed and archived."
   - Terminate

5. **Manager Authorization Check:** Verify the SMS sender is authorized to approve this request:
   - Authorized if ANY of:
     - SMS sender's name matches the leave request's Manager name
     - SMS sender is "Jay Puzon" (admin)
     - SMS sender is "Mandy Leong" (admin)
     - SMS sender is "Dave Powell" (admin)
   - If NOT authorized:
     - Send SMS: "Invalid response - you do not have access to request #{itemid}."
     - Terminate

6. **Actual Manager Resolution:** If the SMS sender is NOT the direct manager (but is an admin):
   - Look up the actual manager's email from SD using the leave request's Manager name
   - Store as `actualmanager` (used in CC for balance emails)

### SystemCheck Wait Loop

Same as flow 04: wait for `SystemCheck != "Editing"` before proceeding.

### Process Approval/Rejection

**If decision == "Approve" AND Status == "Pending":**

1. Update leave request: `Status = "Approved"`, `ApproveProcessedFlag = "Processed"`, `ApprovedDate = today`
2. Send approval email to employee (same template as flow 04)
3. Send SMS confirmation to manager: "Response has been received. An email will be sent to ({managerEmail}) once the process is completed."
4. Check hourly staff → terminate if hourly
5. Deduct balance by leave type (same logic as flow 04)
6. Run balance cascading (same logic as flow 04)
7. Recalculate `Request Allow Date` for this employee (see `05-MAINTENANCE.md`)
8. Send balance update email
9. Set SystemCheck to Ready
10. Log new balances to leave request

**If decision == "Reject" OR Status != "Pending":**

1. Update leave request: `Status = "Rejected"`, `ApproveProcessedFlag = "Processed"`
2. Send rejection email to employee
3. Send SMS confirmation to manager: "Response has been received. Cancelling request #{itemid}."

---

## 6. Bereavement and Jury Duty Alert (Flow: 08 Leave Request Bereavement and Jury Duty Alert)

**Trigger:** When an item is created in the Leave Requests list (polled every 1 minute).

### Logic

1. Check if `LeaveType == "Bereavement"` OR `LeaveType == "Jury Duty"`
2. If YES → Send alert email:
   - **To:** `Mandyl@ucsh.com; generalmail@ucsh.com`
   - **Subject:** "Jury Duty / Bereavement Alert"
   - **Body:**
     ```
     Employee ({SubmitterName}) has leave for {LeaveType}.

     Details:
     {Title}

     {StartDate} to {EndDate}

     Link to request: {SharePoint item link}
     ```
3. If NO → Terminate (do nothing)

---

## Flow Execution Order

When a new leave request is submitted, these flows fire in sequence (each triggered by the previous step's SharePoint write):

```
MS Forms Submission
  └─→ 01 New Leave Request (creates SP item)
        ├─→ 02 Auto Calculate Days (calculates days, may auto-reject)
        ├─→ 03 Auto Manager (assigns manager, location, department)
        └─→ 08 Bereavement/Jury Duty Alert (sends alert if applicable)
              └─→ 04 Leave Request Approval (triggered by manager assignment modification)
                    └─→ Sends approval email to manager
                          └─→ Manager responds (email or SMS via flow 07)
                                └─→ Balance adjustments if approved
                                      └─→ Recalculate Request Allow Date
```

**Note:** Flows 02, 03, and 08 all trigger on "item created" and run in parallel. Flow 04 triggers on "item modified" (specifically when the manager is assigned by flow 03).
