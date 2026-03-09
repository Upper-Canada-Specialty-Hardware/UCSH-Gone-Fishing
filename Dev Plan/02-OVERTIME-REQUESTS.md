# 02 - Overtime Requests

Complete lifecycle of an overtime / make-up time request from submission through approval and balance adjustment.

---

## 1. Submission (Flow: 01 New Overtime Request)

**Trigger:** MS Forms webhook — when a new response is submitted to the Overtime Request form.

### Form Fields Captured

| Form Field | Maps To |
|------------|---------|
| Description/Reason | Title |
| Date | StartDate |
| Hours | Hours (converted to integer) |
| Responder email | SubmittedBy (via Claims) |

### Submission Logic

1. Get form response details
2. Convert hours from form response to integer: `int(hours_field_value)`
3. Store as variable `IntegerValue`
4. Create SharePoint list item in Overtime Requests with:
   - `Title` = description from form
   - `StartDate` = date from form
   - `Hours` = integer hours value
   - `Status` = "Pending"
   - `SubmittedBy` = responder email (Claims)

---

## 2. Auto-Assign Manager (Flow: 02 Overtime Request Auto Manager)

**Trigger:** When an item is created in the Overtime Requests list (polled every 1 minute).

### Logic

1. Look up submitter in Staff Directory: `filter Title eq '{SubmittedBy.DisplayName}'` (top 4999)
2. If no match found → Terminate (flow ends)
3. If match found → Update the overtime request:
   - `Manager` = employee's `Supervisor` field (Claims — this assigns the supervisor as the manager)
   - `Status` = "Pending" (re-confirmed)
   - Preserve `Title`, `StartDate`, `Hours`

---

## 3. Approval (Flow: 03 Overtime Request Approval)

**Trigger:** When an item is modified in the Overtime Requests list (polled every 1 minute).

### Pre-Checks

1. **Status Must Be Pending:** If `Status != "Pending"` → terminate
2. **Manager Must Be Assigned:** If `Manager == null` → terminate

### Employee and Province Lookup

1. Get employee from Staff Directory: `filter Title eq '{SubmittedBy.DisplayName}'`
2. Get employee's full record by ID
3. Map employee's Location to Province (same mapping as leave requests):

| Location | Province |
|----------|----------|
| Toronto Victoria Park | ON |
| Toronto Warden | ON |
| Ottawa | ON |
| Leaside | ON |
| Barrie | ON |
| British Columbia | BC |
| Newfound Land | NL |

### Holiday Check

1. Get all Company Holidays (top 5000)
2. For each holiday: check if `holiday.Date == request.StartDate` AND holiday title does NOT contain "Half Fridays" AND `holiday.Province == employee's province`
3. If any holiday matches → set `varIsHoliday = true` and record the `varHolidayName`

### Half-Day Friday Detection

1. Only check if the overtime date falls on a Friday (`dayOfWeek(StartDate) == 5`)
2. Find "Half Fridays START" and "Half Fridays END" entries from holidays list
3. If `StartDate >= Half Fridays START date` AND `StartDate <= Half Fridays END date` → set `varIsHalfFriday = true`

### Manager Email Lookup

1. Look up manager by name in Staff Directory: `filter Title eq '{Manager.DisplayName}'`
2. Extract manager's email address

### Branching: Holiday vs Non-Holiday

**If Holiday (varIsHoliday == true):**

1. Auto-reject the request:
   - Update overtime request: `Status = "Rejected"`
2. Send rejection email:
   - **To:** Employee email; Manager email
   - **Subject:** "Overtime Request - Auto Rejected"
   - **Body:** "Request for Time Make-Up was auto-rejected by the system. Date requested is a company holiday: {varHolidayName}"
   - Include request details (title, date, hours)
3. Flow ends

**If Not Holiday (varIsHoliday == false):**

#### Sub-Branch: Half-Day Friday vs Regular Day

**If NOT a Half-Day Friday (varIsHalfFriday == false):**

Send approval email with options:
- **To:** Manager email
- **Subject:** "Overtime Request - {SubmitterName}"
- **Options:** "Approve, Reject"
- **Header:** "Time Make-Up Request"
- **Body:**
  ```
  Requested by: {SubmitterName}
  Time Make-Up Date: {StartDate}
  Duration: {Hours} hours
  Details:
  {Title}
  ```

**If IS a Half-Day Friday (varIsHalfFriday == true):**

Send approval email with modified subject:
- **Subject:** "Overtime Request - {SubmitterName} - Half-Day Friday Detected"
- **Body:** Adds "Half-Day Friday detected from the requested date." at the top
- All other fields same as above

---

### Process Approval Response

#### If Approved

1. **Calculate New Overtime Balance:**
   ```
   varNewOvertimeBalance = CurrentOvertimeBalance + (Hours / 8)
   ```
   Hours are converted to days by dividing by 8.

2. **Hourly Staff Check:**
   - If `SalaryHourly == "Hourly"`:
     - Send simplified approval email:
       - **Subject:** "Overtime Approved - Hourly - {EmployeeName}"
       - **Body:** Request details only, no balance information
     - Terminate (no balance adjustment)

3. **Update Employee Balance:**
   - Set `CurrentOvertimeBalance = varNewOvertimeBalance` in Staff Directory

4. **Update Overtime Request:**
   - `Status` = "Approved"
   - `ApprovedDate` = current date (`yyyy-MM-dd`)

5. **Vacation Offset Logic:**
   After updating the overtime balance, check if the employee's vacation is negative and overtime is now positive. If so, offset:

   a. Re-read employee record from SD
   b. If `CurrentVacationBalance < 0`:
      - Compute: `newVacation = CurrentVacationBalance + CurrentOvertimeBalance`
      - Update SD: `CurrentVacationBalance = newVacation`, `CurrentOvertimeBalance = 0`
      - Re-read employee record
      - If `CurrentVacationBalance > 0` (overshot — vacation went positive):
        - Transfer surplus back: `CurrentOvertimeBalance = CurrentVacationBalance`, `CurrentVacationBalance = 0`

6. **Recalculate Request Allow Date** for this employee (see `05-MAINTENANCE.md`)

7. **Send Approval Email:**
   - **To:** Employee email; Manager email
   - **Subject:** "{EmployeeName} Overtime Approved - {Date}"
   - **Body:**
     ```
     Time Make-Up Request Details
     {Title}
     Date: {StartDate}
     Hours: {Hours}
     Approved by: {ManagerName}

     New Balances
     Vacation - {CurrentVacationBalance} Days
     Sick Leave - {CurrentSickDayBalance} Days
     Carry Over - {CarryOver} Days
     Time Make-Up - {CurrentOvertimeBalance} Days
     ```

#### If Rejected

1. Look up employee from SD
2. Send rejection email:
   - **To:** Employee email; Manager email
   - **Subject:** "{EmployeeName} Overtime Rejected - {Date}"
   - **Body:**
     ```
     Time Make-Up Request Details
     {Title}
     Date: {StartDate}
     Hours: {Hours}
     Rejected by: {ManagerName}

     Current Balances
     Vacation - {CurrentVacationBalance} Days
     Sick Leave - {CurrentSickDayBalance} Days
     Carry Over - {CarryOver} Days
     Time Make-Up - {CurrentOvertimeBalance} Hours
     ```
   (Note: Rejection emails show "Hours" for Make-Up, approval emails show "Days")

3. Update overtime request: `Status = "Rejected"`

---

## Flow Execution Order

```
MS Forms Submission
  └─→ 01 New Overtime Request (creates SP item)
        └─→ 02 Auto Manager (assigns manager)
              └─→ 03 Overtime Approval (triggered by manager assignment modification)
                    ├─→ Holiday check → auto-reject if holiday
                    ├─→ Half-Friday detection → modified email subject
                    └─→ Sends approval email to manager
                          └─→ Manager responds
                                ├─→ Approve: balance update + vacation offset + recalculate Request Allow Date
                                └─→ Reject: notification only
```

---

## Key Differences from Leave Requests

| Aspect | Leave Requests | Overtime Requests |
|--------|---------------|-------------------|
| Balance Direction | Days are DEDUCTED from balances | Days are ADDED to overtime balance |
| Conversion | N/A (already in days) | Hours / 8 = days |
| Cascading | Full cascading sequence on approval | No cascading; instead, vacation offset logic |
| Holiday Handling | Partial day auto-reject; holidays excluded from day count | Auto-reject entire request if date is a holiday |
| SMS Approval | Yes (flow 07) | No |
| SystemCheck Locking | Yes | No |
| ApproveProcessedFlag | Yes | No (no double-processing protection) |
