# Staff Directory writers and how the Postgres move covers them (#269)

The Staff Directory SharePoint list is the current source of truth for every
employee. As it moves to Postgres (#261), reads are served from either backend
through the data-access store, and writes must keep the SharePoint list current
(the Bulletin Board views, the Microsoft Form and the Power Automate flows still
read it). This document inventories every code path in this repository that
writes to the Staff Directory list and says how each is covered after the move.
It also lists the questions for the product owner about the Power Automate flows,
which cannot be seen from the code.

## The move, in one paragraph

Reads of the Staff Directory go through `app/repositories/staff_store.py`, which
routes to SharePoint or to the Postgres `staff` table based on the `staff_storage`
app setting (with `STORAGE_EMPLOYEES` as an environment override), exactly like
the request store. The Postgres table answers in the same SharePoint item shape,
so callers do not change. Balance pot writes on approval go through the shared
`StaffBalances` interface; its Postgres implementation
(`app/repositories/postgres/staff_balances.py`) writes the pots in the caller's
own transaction and queues a write-back so the SharePoint list catches up.
Changes made directly in SharePoint are brought into Postgres by the pull
(`app/services/staff_pull.py`), newer edit wins by modified time, recorded in
`staff_changes` with source `sharepoint`, and never echoed back.

## Inventory of Staff Directory writes

Each row is a code path that writes to the Staff Directory list today, the
columns it writes, and how the move covers it.

| Code path | What it writes | Coverage after the move |
|---|---|---|
| `services/employee.py` (reads only) | nothing | Reads now go through `staff_store`, so they follow `staff_storage`. No writes here. |
| `services/balance.py` `cascade_next_year`, `apply_vacation_offset`, `recalculate_request_allow_date` | `CurrentOvertimeBalance`, `CarryOver`, `CurrentVacationBalance`, `RequestAllowDate` | Balance movements. In the Postgres approval path these go through `StaffBalances.write_pots`, which writes Postgres and queues a write-back. The direct `sp_client` writes here are the SharePoint-era path and stay correct while staff are on SharePoint; they are replaced at the approval-path cutover. Staff and requests switch to Postgres together, so this path is never the live writer while staff are in Postgres. |
| `services/leave_requests.py` approval and refund (sick / make-up / sick deductions, reversals) | the five balance pots | Same as `balance.py`: `StaffBalances.write_pots` in the Postgres path; direct writes are the SharePoint-era path. |
| `services/overtime_requests.py` approval and reversal | `CurrentOvertimeBalance` (and offsets) | Same as above. |
| `services/carryover_payout.py` approval and reversal | `CurrentVacationBalance`, `CarryOver`, `Payout` | Same as above. |
| `tasks/carryover_reset.py` (the April-1 reset) | `CurrentVacationBalance`, `CarryOver`, `Payout` | A scheduled balance write. Not yet routed through the store. While staff are on SharePoint it is unchanged. Once staff are in Postgres it should write through `staff_store`/`StaffBalances` so the reset lands in Postgres and replays to SharePoint. Open follow-up. |
| `services/staff_entitlements.py` `apply_entitlement_changes` and `routes/dashboard.py` admin set-balances | entitlements, the five pots, text/choice fields, person links, `AllManagers` | HR person-panel edits. Not yet routed through the store. Interim coverage: the edit writes to SharePoint; the pull reconciles it into Postgres (newer-wins) and records it in `staff_changes`. Open follow-up: route this through `staff_store` so the edit lands in Postgres directly and the write-back replays it, removing the round-trip and the double history record. |
| `services/employee_creation.py` `create_employee` | a new Staff Directory item, then `AllManagers` and `RequestAllowDate` | Creating a person. Not yet routed. Interim coverage: created in SharePoint, imported into Postgres by the pull. Open follow-up: route creation through `staff_store.create_list_item`, which mints the id and queues the write-back. |
| `services/manager_assignments.py` `update_employee_managers` and the bulk operations | `AllManagers` | Manager links. Not yet routed. Interim coverage: written to SharePoint, pulled into Postgres (the links rebuild from the `AllManagers` blob). Open follow-up: route through `staff_store`. |

Notes:
- `tasks/setup_nudges.py` uses the Staff Directory list id only as an idempotency
  namespace for its daily sweep (`claim_action`); it does not write staff fields.
- `app/repositories/sharepoint/employee.py` is the SharePoint side of the
  repository seam and only writes when a caller asks it to; no caller writes
  through it today.

## What is covered now, and what is a follow-up

Covered by this work:
- All Staff Directory reads in `services/employee.py` route through `staff_store`.
- Balance pot reads and writes on the Postgres approval path go through
  `StaffBalances`, which keeps the move in one transaction and queues a
  write-back.
- Changes made directly in SharePoint are pulled into Postgres, newer-wins, with
  a `staff_changes` record and no echo.
- The write-back drains to SharePoint with retries so other systems stay current.

Open follow-ups (each is small, and each writer above names its own):
- Route the HR person-panel edit, employee creation, manager assignment and the
  carryover reset through `staff_store` so their writes land in Postgres and the
  write-back replays them, rather than relying on the pull to reconcile.
- The legacy direct balance writes are replaced at the approval-path cutover; the
  owner's rule that staff and requests switch to Postgres together is what keeps
  this safe in the meantime.

## Questions for the product owner about the Power Automate flows

The flows cannot be read from the code, so the inventory above is only the code's
own writers. The code strongly implies at least one flow, and there are likely
more that touch staff fields. Please confirm each so the write-back and pull
cover them correctly:

1. **The request intake flow.** A Microsoft Form submission becomes a SharePoint
   request-list item through a Power Automate flow. Does that flow ever write to
   the Staff Directory list (for example, filling a submitter person field, or
   creating a staff record for a new hire), or only to the request lists?
2. **Onboarding / identity sync.** Is there a flow that creates or updates Staff
   Directory records from another source, such as Entra / on-prem Active
   Directory, HR onboarding, or a joiner-mover-leaver process? If so, which
   columns does it write, and how often?
3. **Person links.** Is there a flow that populates `TitleLink` or
   `SupervisorLink` (the Microsoft 365 person columns) when someone first visits
   the site or is added to it?
4. **Manager / org chart.** Is there a flow that maintains `AllManagers` or
   `SupervisorLink` from an org chart or a manager-change request?
5. **Balances and entitlements.** Is there any flow (or an external app such as
   payroll) that writes the balance or entitlement columns on a schedule or on an
   event, separate from this app's approval and April-1 reset?
6. **Birthday / anniversary / housekeeping.** Are there smaller automations that
   write any Staff Directory column (for example, a derived field or a status)?

For every flow that writes a staff field, the move needs to know: which columns,
what triggers it, and whether it should keep writing SharePoint (the write-back
and pull will carry it) or be pointed at the app instead. A flow that writes a
column the typed `staff` table does not model would be reported by the import as a
dropped column, so please flag any custom columns a flow depends on.
