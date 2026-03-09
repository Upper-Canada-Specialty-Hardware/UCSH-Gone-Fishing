# 03 - CarryOver and Payout Requests

Complete lifecycle of a balance transfer request — either carrying over vacation days to the next year, or converting vacation days to payout.

---

## 1. Submission (Flow: 01 New CarryOver Payout Request)

**Trigger:** MS Forms webhook — when a new response is submitted to the CarryOver/Payout form.

### Form Fields Captured

| Form Field | Maps To |
|------------|---------|
| Request Type | "Carry Over" or "Payout" (determines TypeofRequest) |
| Carry Over Days | Days (if Carry Over selected) |
| Payout Days | Days (if Payout selected) |
| Responder email | SubmittedBy (via Claims) |

### Submission Logic

1. Get form response details
2. Initialize variables: `CarryOverDays` and `PayoutDays` from form fields
3. Check if request type is "Carry Over":

**If Carry Over:**
- Create SharePoint list item in CarryOver Payout Data with:
  - `TypeofRequest` = "Carry Over"
  - `Days` = float(CarryOverDays)
  - `SubmittedBy` = responder email (Claims)
  - `SystemState` = "Not Processed"

**If Payout:**
- Create SharePoint list item with:
  - `TypeofRequest` = "Payout"
  - `Days` = float(PayoutDays)
  - `SubmittedBy` = responder email (Claims)
  - (SystemState not explicitly set — defaults)

---

## 2. Auto-Assign Manager (Flow: 02 CarryOver Payout Auto Manager)

**Trigger:** When an item is created in the CarryOver Payout list (polled every 1 minute).

### Logic

1. Look up employee in Staff Directory by email: `filter EmailAddress eq '{SubmittedBy.Email}'`
2. Extract manager name: `employee.Supervisor`
3. Extract employee ID: `employee.ID`
4. Look up manager in Staff Directory: `filter Title eq '{Supervisor}'`
5. Extract manager ID: `manager.ID`
6. Get full employee record by ID
7. Get full manager record by ID
8. Update the CarryOver Payout request with:
   - `Manager` = manager's email (Claims)
   - `Managertxt` = manager's display name
   - `SystemState` = "Not Processed"
   - `EmployeeID` = employee's SharePoint list item ID
   - `ManagerID` = manager's SharePoint list item ID

---

## 3. Approval (Flow: 03 CarryOver Payout Request Approval)

**Trigger:** When an item is modified in the CarryOver Payout list (polled every 1 minute).

### Pre-Checks

1. **Manager Must Be Set:** If `Managertxt == null` → terminate (auto-manager hasn't run)
2. **Must Not Be Already Processed:** If `SystemState != "Not Processed"` → terminate
3. Set `SystemState = "Processing"` to prevent duplicate processing

### Employee and Manager Lookup

1. Extract `EmployeeID` and `ManagerID` from the request
2. Get manager record from SD by ID
3. Get employee record from SD by ID
4. Initialize variables:
   - `CurrentVacation` = employee's `CurrentVacationBalance`
   - `CurrentCarryOver` = employee's `CarryOver`
   - `CurrentPayout` = employee's `Payout`

### Pre-Validation

**Step 1: Compute new vacation balance (common to both types):**
```
NewCurrentVacation = CurrentVacation - Days
```

**Step 2: Type-specific validation:**

**If Carry Over:**
```
NewCarryOver = CurrentCarryOver + Days
```

**If Payout:**
```
NewPayout = CurrentPayout + Days
```
- **Payout Cap Check:** If `NewPayout > 5`:
  - Auto-reject: Update request `Title = "System Auto-Rejected: new Payout value will exceed 5."`, `Status = "Rejected"`, `SystemState = "Processed"`
  - Send email to employee:
    - **Subject:** "Payout Request - Auto Rejected"
    - **Body:** "Please note that your Payout Request #{ID} has been auto-rejected by the system. You may only request for a maximum for a total of 5 Payout days for the current calendar year."
  - Terminate

**Step 3: Vacation Cannot Go Negative:**
- If `NewCurrentVacation < 0`:
  - Send email to employee:
    - **Subject:** "Carry Over / Payout Request - Auto Rejected"
    - **Body:** Includes current balances and explains: "Company Policy - Payout can not exceed 5 days. Current Vacation can not result in negative days."
  - Update request: `Status = "Rejected"`, `SystemState = "Processed"`
  - Terminate

### Send Confirmation to Employee

**If Carry Over:**
- **Subject:** "Request Received for Carry Over"
- **Body:** Request details, current balances, projected new balances if approved:
  ```
  If approved, your balances will be adjusted to:
  Vacation - ({NewCurrentVacation}) days
  Carry Over - ({NewCarryOver}) days
  Payout - ({CurrentPayout}) days
  ```

**If Payout:**
- **Subject:** "Request Received for Payout"
- **Body:** Same structure, with payout projections:
  ```
  If approved, your balances will be adjusted to:
  Vacation - ({NewCurrentVacation}) days
  Carry Over - ({CurrentCarryOver}) days
  Payout - ({NewPayout}) days
  ```

### Send Approval Email to Manager

**If Carry Over:**
- **To:** Manager email; `mandyl@ucsh.com`
- **Subject:** "Carry Over Request #{ID} Submitted by {EmployeeName}"
- **Options:** "Approve, Reject"
- **Body:** Employee name, requested days, current balances, projected new balances

**If Payout:**
- **To:** Manager email; `mandyl@ucsh.com`
- **Subject:** "Payout Request #{ID} Submitted by {EmployeeName}"
- **Options:** "Approve, Reject"
- **Body:** Same structure with payout projections

### Random Delay (Carry Over only)

After the manager responds, the flow waits a random delay of 1–900 seconds before proceeding. This helps avoid race conditions when multiple requests are approved simultaneously.

---

### Process Carry Over Approval

**If Approved:**

1. **SystemCheck Wait Loop:**
   - Poll employee's SD record in a Do-Until loop (max 60 iterations)
   - Wait for `SystemCheck == "Ready"`, with 1-minute delay between checks

2. **Re-read Employee Balances** (balances may have changed since submission):
   - Get fresh employee record from SD
   - Recalculate: `FinalCurrentVacation = freshVacation - Days`
   - Recalculate: `FinalCarryOver = freshCarryOver + Days`

3. **Re-Validate at Approval Time:**
   - If `FinalCurrentVacation < 0`:
     - Send override email to employee, manager, and `mandyl@ucsh.com`:
       - **Subject:** "System Override Reject: Carry Over Request #{ID} Submitted by {EmployeeName}"
       - **Body:** "Employee no longer has a sufficient vacation balance to accommodate the initial carry over / payout request at the time of manager's approval."
     - Update request: `Status = "Rejected"`, `SystemState = "Processed"`
     - Terminate

4. **Apply Balance Transfer:**
   - Set `SystemCheck = "Editing"` on employee's SD record
   - Update employee SD:
     - `CurrentVacationBalance = FinalCurrentVacation`
     - `CarryOver = FinalCarryOver`

5. **Recalculate Request Allow Date** for this employee (see `05-MAINTENANCE.md`)

6. **Set `SystemCheck = "Ready"`** on employee's SD record

7. **Update Request:**
   - `Status = "Approved"`
   - `SystemState = "Processed"`
   - `NewBalance = "{Vacation:{val}, CarryOver:{val}, Payout:{val}}"`

8. **Send Approval Email to Employee:**
   - **To:** Employee email
   - **CC:** Manager email
   - **Subject:** "Carry Over Request #{ID} Approved"
   - **Body:** Updated balances (Vacation, Sick, Carry Over, Make Up, Payout)
   - **Importance:** High

**If Rejected:**

1. Update request: `Status = "Rejected"`, `SystemState = "Processed"`
2. Send rejection email to employee:
   - **CC:** Manager email
   - **Subject:** "Carry Over Request #{ID} Rejected"
   - **Body:** Request details, "No changes have been made to your existing balance."

---

### Process Payout Approval

**If Approved:**

1. **SystemCheck Wait Loop:** Same as carry over

2. **Re-read Employee Balances:**
   - Get fresh employee record
   - `FinalCurrentVacation = freshVacation - Days`
   - `FinalPayout = freshPayout + Days`

3. **Re-Validate at Approval Time:**
   - If `FinalCurrentVacation < 0`:
     - Send override email (same as carry over)
     - Reject and terminate

4. **Apply Balance Transfer:**
   - Set `SystemCheck = "Editing"`
   - Update employee SD:
     - `CurrentVacationBalance = FinalCurrentVacation`
     - `Payout = FinalPayout`

5. **Recalculate Request Allow Date** for this employee (see `05-MAINTENANCE.md`)

6. **Set `SystemCheck = "Ready"`** on employee's SD record

7. **Update Request:**
   - `Status = "Approved"`
   - `SystemState = "Processed"`
   - `NewBalance = "{Vacation:{val}, CarryOver:{val}, Payout:{val}}"`

8. **Send Approval Email to Employee:**
   - **To:** Employee email
   - **CC:** Manager email
   - **Subject:** "Payout Request #{ID} Approved"
   - **Body:** Updated balances
   - **Importance:** High

**If Rejected:**

1. Update request: `Status = "Rejected"`, `SystemState = "Processed"`
2. Send rejection email to employee:
   - **CC:** Manager email
   - **Subject:** "Payout Request #{ID} Rejected"
   - **Body:** Request details, "No changes have been made to your existing balance."

---

## Flow Execution Order

```
MS Forms Submission
  └─→ 01 New CarryOver Payout Request (creates SP item)
        └─→ 02 Auto Manager (assigns manager + IDs)
              └─→ 03 Approval (triggered by manager assignment modification)
                    ├─→ Pre-validation (payout cap, vacation negative check)
                    ├─→ Employee confirmation email
                    ├─→ Manager approval email
                    └─→ Manager responds
                          ├─→ Approve: re-validate → SystemCheck lock → balance transfer → recalculate Request Allow Date → notify
                          └─→ Reject: notify only
```

---

## Key Differences from Leave Requests

| Aspect | Leave Requests | CarryOver/Payout |
|--------|---------------|------------------|
| Balance Direction | Days deducted from balances | Days transferred between Vacation ↔ CarryOver/Payout |
| Cascading | Yes (multi-step) | No (direct transfer) |
| Double Validation | No | Yes (at submission AND at approval time) |
| SystemState Field | ApproveProcessedFlag | SystemState (Not Processed / Processing / Processed) |
| Random Delay | No | Yes (1–900 seconds for carry over) |
| Admin CC | No | Yes (mandyl@ucsh.com on approval emails) |
| Payout Cap | N/A | 5 days maximum total |
