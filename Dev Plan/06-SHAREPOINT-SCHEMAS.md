# 06 - SharePoint List Schemas

All data resides on the SharePoint site: `https://ucshca.sharepoint.com/sites/UCSHBulletinBoard`

---

## 1. Staff Directory (`Staff_Directory_Data`)

**List GUID:** `ed4bba96-f035-4eee-a8ff-af71036034fe`

This is the central employee record. All balance tracking, manager assignments, and employee metadata live here.

| Column | Internal Name | Type | Business Meaning |
|--------|--------------|------|-----------------|
| Title | Title | Single line of text | Employee full name (display name). Used as lookup key across all flows. |
| TitleLink | TitleLink | Person or Group | Employee as a SharePoint person reference. |
| Email Address | EmailAddress | Single line of text | Employee email. Used for all notification emails. |
| Cell Number | CellNumber | Single line of text | Employee cell phone. Used for SMS approval matching (last 10 digits). |
| Extension | Extension | Number | Office phone extension. |
| Birthday | Birthday | Date and Time | Employee birthday. |
| Location | Location | Choice | Office location. Values: `Toronto Victoria Park`, `Toronto Warden`, `Ottawa`, `Leaside`, `Barrie`, `British Columbia`, `Newfound Land`. Used to determine province for holiday filtering. |
| Department | Department | Choice | Employee department. Stamped onto leave requests. |
| Salary Hourly | SalaryHourly | Choice | Values: `Salary`, `Hourly`. Hourly staff skip all balance adjustments on approval. |
| Supervisor | Supervisor | Single line of text | Manager's full name (plain text). Used to look up the manager's SD record. |
| SupervisorLink | SupervisorLink | Person or Group | Manager as a SharePoint person reference. |
| AllManagers | AllManagers | Person or Group (multi) | Full management chain. Copied to leave requests for visibility. |
| Vacation Entitlement | DefaultYearlyVacationDays | Number | Annual vacation entitlement in days. |
| Current Vacation Balance | CurrentVacationBalance | Number | Remaining vacation days for the current year. Can go negative during cascading. |
| Sick Day Entitlement | SickDayEntitlement | Number | Annual sick day entitlement. |
| Current Sick Day Balance | CurrentSickDayBalance | Number | Remaining sick days. Can go negative during cascading. |
| Carry Over Balance | CarryOver | Number | Days carried over from previous year. Consumed before vacation in cascading. |
| Current Make Up Balance | CurrentOvertimeBalance | Number | Overtime / make-up time balance in days (hours / 8). Consumed first in cascading. |
| Payout | Payout | Number | Accumulated payout days. Maximum 5 per calendar year. |
| System Check | SystemCheck | Choice | Values: `Ready`, `Editing`. Locking mechanism to prevent concurrent balance updates. |
| Request Allow Date | RequestAllowDate | Date and Time | Calculated daily by maintenance flow. Controls how far into the future an employee can request leave. |
| Comments | Comments | Multiple lines of text | Free-text HR comments. Included in EOM manager reports. |
| Modified | Modified | Date and Time | Auto-set by SharePoint. |
| Created | Created | Date and Time | Auto-set by SharePoint. |
| Created By | Created By | Person or Group | Auto-set. |
| Modified By | Modified By | Person or Group | Auto-set. |

---

## 2. Leave Requests (`Leave_Requests`)

**List GUID:** `bd21037a-9c3c-4682-9aaa-948095e16aec`

Each row represents one leave request submitted via MS Forms.

| Column | Internal Name | Type | Business Meaning |
|--------|--------------|------|-----------------|
| Title | Title | Single line of text | For standard requests: `{FirstName LastName} /// {optional notes}`. For partial day: employee name from form. |
| Submitted By | SubmittedTest | Person or Group | The employee who submitted the form (set via Claims from responder email). |
| Leave Type | LeaveType | Choice | Values: `Vacation`, `Sick or Personal Day`, `Bereavement`, `Jury Duty`, `Half Day or Partial Day Off`. |
| Start Date | StartDate | Date and Time | First day of leave. |
| End Date | EndDate | Date and Time | Last day of leave. Same as Start Date for partial days. |
| Days | Days | Number | Calculated business days. For partial days: hours / 8. Excludes weekends, holidays, and deducts 0.5 for half-day Fridays. |
| Status | Status | Choice | Values: `Pending`, `Approved`, `Rejected`. |
| Approve Processed Flag | ApproveProcessedFlag | Choice | Values: `Not Processed`, `Processed`. Prevents double-processing of approvals. |
| Approved Date | ApprovedDate | Date and Time | Date the request was approved. Set to `yyyy-MM-dd` format of current UTC time. |
| Manager | Manager | Person or Group | Assigned manager (looked up from SD via Supervisor field). |
| Managertxt | Managertxt | Single line of text | Manager's display name (plain text). Used for filtering in reports. |
| AllManagers | AllManagers | Person or Group (multi) | Full management chain copied from employee's SD record. |
| Staff Location | StaffLocation | Choice | Employee's office location, stamped from SD. |
| Staff Department | StaffDepartment | Choice | Employee's department, stamped from SD. |
| New Balances | NewBalances | Single line of text | Snapshot of balances after approval processing. Format: `(Vacation:X)(Sick:Y)(CarryOver:Z)(Make-Up:W)` |
| Approve | Approve | Hyperlink or Picture | Approve action link (used in legacy email approval). |
| Reject | Reject | Hyperlink or Picture | Reject action link (used in legacy email approval). |
| Modified | Modified | Date and Time | Auto-set. |
| Created | Created | Date and Time | Auto-set. |
| Created By | Created By | Person or Group | Auto-set. |
| Modified By | Modified By | Person or Group | Auto-set. |

---

## 3. Overtime Requests (`Overtime_Requests_Data`)

**List GUID:** `1ea6f753-5c84-450f-b266-707d73c71133`

Each row represents one overtime/make-up time request.

| Column | Internal Name | Type | Business Meaning |
|--------|--------------|------|-----------------|
| Title | Title | Single line of text | Description/reason for overtime from the form. |
| Date | StartDate | Date and Time | The date the overtime was worked. (Internal name is `StartDate` but displayed as "Date".) |
| Hours | Hours | Number | Number of overtime hours (integer, converted from form). |
| Submitted By | SubmittedBy | Person or Group | Employee who submitted (set via Claims from responder email). |
| Manager | Manager | Person or Group | Assigned manager (looked up from SD Supervisor field). |
| Status | Status | Choice | Values: `Pending`, `Approved`, `Rejected`. |
| Approved Date | ApprovedDate | Date and Time | Date approved. Set to `yyyy-MM-dd` format. |
| Modified | Modified | Date and Time | Auto-set. |
| Created | Created | Date and Time | Auto-set. |
| Created By | Created By | Person or Group | Auto-set. |
| Modified By | Modified By | Person or Group | Auto-set. |

---

## 4. CarryOver / Payout Requests (`CarryOver_Payout_Data`)

**List GUID:** `bcfd8cc6-b29f-4d6f-a449-cb1d0a9251bb`

Each row represents a balance transfer request (carry over or payout).

| Column | Internal Name | Type | Business Meaning |
|--------|--------------|------|-----------------|
| Title | Title | Single line of text | Auto-set on rejection to describe reason (e.g., "System Auto-Rejected: new Payout value will exceed 5."). |
| Submitted By | SubmittedBy | Person or Group | Employee who submitted (set via Claims from responder email). |
| Manager | Manager | Person or Group | Assigned manager (looked up from SD). |
| Managertxt | Managertxt | Single line of text | Manager name (plain text). |
| Type of Request | TypeofRequest | Choice | Values: `Carry Over`, `Payout`. |
| Days | Days | Number | Number of days to transfer from vacation balance. |
| Status | Status | Choice | Values: `Pending`, `Approved`, `Rejected`. |
| System State | SystemState | Choice | Values: `Not Processed`, `Processing`, `Processed`. Prevents duplicate processing. Flow sets to "Processing" before sending approval email, "Processed" after completion. |
| EmployeeID | EmployeeID | Number | SharePoint list item ID of the employee in Staff Directory. Set by auto-manager flow. |
| ManagerID | ManagerID | Number | SharePoint list item ID of the manager in Staff Directory. Set by auto-manager flow. |
| NewBalance | NewBalance | Single line of text | Snapshot of balances after approval. Format: `{Vacation:X, CarryOver:Y, Payout:Z}` |
| Modified | Modified | Date and Time | Auto-set. |
| Created | Created | Date and Time | Auto-set. |
| Created By | Created By | Person or Group | Auto-set. |
| Modified By | Modified By | Person or Group | Auto-set. |

---

## 5. Company Holidays (`Company_Holidays`)

**List GUID:** `391e299d-c537-44c4-90c8-462e2ca2db5f`

Contains all company holidays plus special "Half Fridays" start/end markers.

| Column | Internal Name | Type | Business Meaning |
|--------|--------------|------|-----------------|
| Title | Title | Single line of text | Holiday name. Special values: `Half Fridays START` and `Half Fridays END` mark the half-day Friday season boundaries. |
| Date | Date | Date and Time | The date of the holiday, or the start/end date for half-friday season. |
| Province | Province | Choice | Values: `ON`, `BC`, `NL`. Holidays are province-specific. Employees are matched to province via their Location. |
| Notes: | Notes | Single line of text | Optional notes about the holiday. |
| Modified | Modified | Date and Time | Auto-set. |
| Created | Created | Date and Time | Auto-set. |
| Created By | Created By | Person or Group | Auto-set. |
| Modified By | Modified By | Person or Group | Auto-set. |

---

## Key Relationships

```
Staff_Directory_Data (central)
  ├── Leave_Requests.SubmittedTest → SD.Title (employee lookup)
  ├── Leave_Requests.Managertxt → SD.Title (manager lookup)
  ├── Overtime_Requests_Data.SubmittedBy → SD.Title (employee lookup)
  ├── Overtime_Requests_Data.Manager → SD.Supervisor (manager lookup)
  ├── CarryOver_Payout_Data.EmployeeID → SD.ID (direct ID reference)
  ├── CarryOver_Payout_Data.ManagerID → SD.ID (direct ID reference)
  └── Company_Holidays.Province ← derived from SD.Location
```

## SharePoint Site URL

All lists are under: `https://ucshca.sharepoint.com/sites/UCSHBulletinBoard`

## SharePoint Document Libraries Referenced

- **Out of Office Log Report Template:** `/Shared Documents/Out of Office Log Report/Template/Manager LR Log Data Template.xlsx`
- **Monthly SD Archive:** `/Shared Documents/dont touch/SD Archive/`
- **Manager Report Output:** `/Shared Documents/Out of Office Log Report/`
