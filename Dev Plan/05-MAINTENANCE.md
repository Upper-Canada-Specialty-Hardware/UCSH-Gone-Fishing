# 05 - Maintenance

## Request Allow Date — Integrated Recalculation

> **Design Change:** The original Power Automate system ran a daily scheduled flow at midnight to recalculate Request Allow Date for all employees. In the new system, Request Allow Date is recalculated **inline after any balance change** for the affected employee only. There is no scheduled job.

### Purpose

The `Request Allow Date` field in the Staff Directory controls how far into the future an employee is allowed to submit leave requests. It is recalculated whenever the employee's `CurrentVacationBalance` or `CarryOver` values change.

---

### When It Runs

Request Allow Date is recalculated at the end of every flow that modifies an employee's balances:

| Flow | Trigger for Recalculation |
|------|--------------------------|
| Leave Request Approval (email or SMS) | After balance cascading completes, before setting SystemCheck=Ready |
| Overtime Request Approval | After vacation offset logic completes |
| CarryOver/Payout Approval | After balance transfer completes, before setting SystemCheck=Ready |

It runs only for the **specific employee** whose balances were just modified.

---

### Logic

#### Step 1: Compute Reference Dates

At runtime, calculate three dates:

| Variable | Formula | Example (if today is 2026-03-03) |
|----------|---------|----------------------------------|
| `end of next year march` | Next year, March 31 | `2027-03-31` |
| `end of current year` | Current year, December 31 | `2026-12-31` |
| `end of next year` | Next year, December 31 | `2027-12-31` |

#### Step 2: Evaluate Employee's Balances

Read the employee's current `CurrentVacationBalance` and `CarryOver` values (already available — just updated by the calling flow), then apply the following rules **in order** (first match wins):

| Condition | Request Allow Date |
|-----------|--------------------|
| Vacation == 0 AND CarryOver != 0 | `end of next year march` (e.g., 2027-03-31) |
| CarryOver == 0 AND Vacation != 0 | `end of current year` (e.g., 2026-12-31) |
| Vacation == 0 AND CarryOver == 0 | `end of next year` (e.g., 2027-12-31) |
| Vacation != 0 AND CarryOver != 0 | No change (keep existing value) |

#### Step 3: Update Only If Changed

Only issue a SharePoint update if the computed `Request Allow Date` differs from the current value stored in the list. This minimizes unnecessary writes.

When updating, the `Title` and `Supervisor` fields must be included in the patch to avoid clearing them (SharePoint PatchItem behavior).

---

### Implementation Notes

- This is a shared utility function called by all three approval pipelines (leave, overtime, carryover/payout)
- The employee's fresh balances are already in memory from the preceding balance update — no extra SharePoint read needed
- No emails or notifications are sent by this logic
- Hourly staff: since their balances are never modified, Request Allow Date recalculation is skipped for hourly employees
