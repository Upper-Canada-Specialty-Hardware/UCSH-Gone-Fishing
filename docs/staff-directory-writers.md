# Staff Directory writers and how the Postgres move covers them (#269)

The Staff Directory SharePoint list is the source of truth for every employee
until the switch. At the switch it moves to Postgres (#261), and from then on
Postgres is the only copy: the app never reads or writes the SharePoint list
again. This document inventories every code path in this repository that writes
staff data and says where each write lands before and after the switch.

## The move, in one paragraph

Reads of the Staff Directory go through `app/repositories/staff_store.py`, which
routes to SharePoint or to the Postgres `staff` table based on the `staff_storage`
app setting (with `STORAGE_EMPLOYEES` as an environment override), exactly like
the request store. The Postgres table answers in the same SharePoint item shape,
so callers do not change. Balance pot writes on approval go through the shared
`StaffBalances` interface; its Postgres implementation
(`app/repositories/postgres/staff_balances.py`) writes the pots in the caller's
own transaction.

## Before and after the switch

Before the switch, staff live in SharePoint. Staff edits made in the admin
dashboard write the SharePoint list, and the shadow sync copies the list into
Postgres every five minutes, with a final copy at the switch itself.

After the switch, Postgres is the **only** source of truth for staff. Every staff
edit in the admin and manager dashboards (the person panel, set-balances, add
employee, manager assignments) and every balance move on approval or refund goes
through `staff_store` and lands in Postgres, with its change recorded in
`staff_changes`. Nothing is written back to SharePoint, and nothing is pulled from
it: an edit made directly in the SharePoint list after the switch is not seen by
the app (ruled 2026-10-18). The old write-back drain, the SharePoint staff pull
and the post-switch SharePoint check were removed for that reason; their tables
(`staff_writeback`, `sharepoint_seen`) are kept, unused, so no schema change was
needed.

## Inventory of Staff Directory writes

Each row is a code path that writes to the Staff Directory list today, the
columns it writes, and how the move covers it.

| Code path | What it writes | Coverage after the move |
|---|---|---|
| `services/employee.py` (reads only) | nothing | Reads now go through `staff_store`, so they follow `staff_storage`. No writes here. |
| `services/balance.py` `cascade_next_year`, `apply_vacation_offset`, `recalculate_request_allow_date` | `CurrentOvertimeBalance`, `CarryOver`, `CurrentVacationBalance`, `RequestAllowDate` | Balance movements. In the Postgres approval path these go through `StaffBalances.write_pots`, which writes Postgres. The direct `sp_client` writes here are the SharePoint-era path and stay correct while staff are on SharePoint; they are replaced at the approval-path cutover. Staff and requests switch to Postgres together, so this path is never the live writer while staff are in Postgres. |
| `services/leave_requests.py` approval and refund (sick / make-up / sick deductions, reversals) | the five balance pots | Same as `balance.py`: `StaffBalances.write_pots` in the Postgres path; direct writes are the SharePoint-era path. |
| `services/overtime_requests.py` approval and reversal | `CurrentOvertimeBalance` (and offsets) | Same as above. |
| `services/carryover_payout.py` approval and reversal | `CurrentVacationBalance`, `CarryOver`, `Payout` | Same as above. |
| `tasks/carryover_reset.py` (the April-1 reset) | `CurrentVacationBalance`, `CarryOver`, `Payout` | A scheduled balance write. **Now routed through `staff_store` (#269):** it reads the staff list and writes `CarryOver` through the store, so on Postgres the reset lands there and records one `system` history row; on SharePoint it is unchanged. `RequestAllowDate` is still written by `balance.py`'s `recalculate_request_allow_date`, which moves onto the balance interface at the approval-path cutover. |
| `services/staff_entitlements.py` `apply_entitlement_changes` and `routes/dashboard.py` admin set-balances | entitlements, the five pots, text/choice fields, person links, `AllManagers` | HR person-panel edits. **Now routed through `staff_store` (#269).** `apply_entitlement_changes` records its own history, so the store is told not to log it a second time (`history_source` left unset), and it passes the manager/supervisor names alongside the id-only Graph payload so the Postgres links keep their names. The older `set-balances` handler does not self-record, so it asks the store to log the correction (`admin_dashboard`). On Postgres each edit lands there and records history once; on SharePoint it is unchanged. |
| `services/employee_creation.py` `create_employee` | a new Staff Directory item, then `AllManagers` and `RequestAllowDate` | Creating a person. **Now routed through `staff_store.create_list_item` (#269):** on Postgres it mints the new staff id in the app's own range and writes nothing to SharePoint; on SharePoint it creates the item directly. The managers are set through `update_employee_managers` (also routed); `RequestAllowDate` is still `balance.py`'s to write. |
| `services/manager_assignments.py` `update_employee_managers` and the bulk operations | `AllManagers` | Manager links. **Now routed through `staff_store` (#269):** reads and the write go through the store, the manager names are passed alongside the id collection so the Postgres links keep their names, and the store records one `system` history row. The bulk operations call `update_employee_managers`, so they are covered too. On SharePoint it is unchanged. |

Notes:
- `app/repositories/sharepoint/employee.py` is the SharePoint side of the
  repository seam and only writes when a caller asks it to; no caller writes
  through it today.

## What is covered now, and what is a follow-up

Covered by this work:
- All Staff Directory reads in `services/employee.py` route through `staff_store`.
- Balance pot reads and writes on the Postgres approval path go through
  `StaffBalances`, which keeps the move in one transaction.
- The HR person-panel edit, the admin set-balances handler, add employee, the
  manager assignment links and the April-1 carry-over reset all route through
  `staff_store` (#269), so on Postgres each write lands there and records its
  change in `staff_changes` once (with source
  `admin_dashboard` or `system`); on SharePoint each is byte-for-byte unchanged.
  History is never doubled: where a writer records its own history
  (`staff_entitlements` and the #233 edit path) the store is told not to log it
  again.
- Add employee on Postgres mints a new staff id in the app's own range (never
  one SharePoint could hand out) and writes nothing to SharePoint.
- Nothing is written back to SharePoint and nothing is pulled from it after the
  switch.

The older balance writes in `balance.py` and `carryover_payout.py` (including
`recalculate_request_allow_date`, which writes `RequestAllowDate`) also go
through `staff_store`, so on Postgres they land in the `staff` table like every
other change. Once requests and staff
are both in Postgres, approvals use the ledger path (`request_decisions`) instead,
and these older functions only serve the SharePoint mode.

## Power Automate

Answered by the product owner on 2026-10-18: there is one flow, and it only turns
a Microsoft Form submission into a SharePoint request-list item. It never writes
the Staff Directory. The Form and the flow stay on after the switch, but the app
no longer reads SharePoint, so anything submitted through them after the switch
is not taken in; requests are made on the request page instead.
