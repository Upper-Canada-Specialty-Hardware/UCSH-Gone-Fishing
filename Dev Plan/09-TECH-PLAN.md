# 09 - Tech Plan

## Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.12+ |
| Framework | FastAPI (async) |
| Database | SQLite (local, via aiosqlite + SQLAlchemy async) |
| Deployment | Railway (standalone project, auto-deploy from GitHub) |
| Source control | Separate GitHub repo |
| SharePoint access | Microsoft Graph API (client credentials OAuth 2.0) |
| Email | Graph API (send as hr@ucshca.onmicrosoft.com) |
| SMS | Twilio REST API + inbound webhook receiver |
| Change detection | SharePoint webhooks + delta queries |
| Approval mechanism | HMAC-SHA256 signed email links + Twilio SMS parsing |
| Background processing | asyncio (in-process tasks) |

---

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `AZURE_TENANT_ID` | Microsoft Entra tenant ID |
| `AZURE_CLIENT_ID` | App registration client ID |
| `AZURE_CLIENT_SECRET` | App registration client secret |
| `TWILIO_ACCOUNT_SID` | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_PHONE_NUMBER` | Twilio sender number (`+16476977133`) |
| `APPROVAL_LINK_SECRET` | HMAC secret for signing approve/reject URLs |
| `BASE_URL` | Public server URL (e.g., `https://gone-fishing.up.railway.app`) |

---

## Project Structure

```
ucsh-gone-fishing/
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app, startup/shutdown, lifespan
│   ├── config.py                # Environment variable loading + validation
│   ├── database.py              # SQLite setup (async engine, session factory, models)
│   │
│   ├── models/                  # SQLAlchemy models (local DB tables)
│   │   ├── __init__.py
│   │   ├── webhook_subscription.py   # SP webhook subscription tracking
│   │   ├── change_token.py           # Delta change tokens per list
│   │   └── processing_log.py         # Idempotency log (processed item IDs)
│   │
│   ├── graph/                   # Microsoft Graph API client layer
│   │   ├── __init__.py
│   │   ├── auth.py              # Token acquisition + caching + auto-refresh
│   │   ├── client.py            # Base HTTP client (httpx async)
│   │   ├── sharepoint.py        # SharePoint list CRUD (items, fields, delta queries)
│   │   ├── email.py             # Send email via Graph API
│   │   └── webhooks.py          # Create, renew, delete SP webhook subscriptions
│   │
│   ├── services/                # Business logic (one module per domain)
│   │   ├── __init__.py
│   │   ├── employee.py          # Staff Directory lookups, province mapping, balance helpers
│   │   ├── holidays.py          # Company holidays, half-friday season logic
│   │   ├── business_days.py     # Business day calculation (weekends, holidays, half-fridays)
│   │   ├── balance.py           # Balance cascading (current year + next year), Request Allow Date
│   │   ├── system_check.py      # SystemCheck lock acquire/release with async polling
│   │   ├── leave_requests.py    # Leave request pipeline (auto-calc, auto-manager, approval)
│   │   ├── overtime_requests.py # Overtime request pipeline (holiday check, approval, vacation offset)
│   │   ├── carryover_payout.py  # CarryOver/Payout pipeline (validation, approval, balance transfer)
│   │   └── approval_links.py    # HMAC link generation + validation
│   │
│   ├── routes/                  # FastAPI route handlers
│   │   ├── __init__.py
│   │   ├── webhooks.py          # POST /api/webhooks/sharepoint — receives SP change notifications
│   │   ├── approval.py          # GET /api/leave/approve/{id}, /api/leave/reject/{id}, etc.
│   │   ├── twilio.py            # POST /api/twilio/sms — Twilio inbound SMS webhook
│   │   └── health.py            # GET /health — health check for Railway
│   │
│   └── tasks/                   # Background async tasks
│       ├── __init__.py
│       ├── change_processor.py  # Processes SP webhook notifications (query delta, dispatch)
│       ├── subscription_manager.py  # Auto-renew SP webhook subscriptions
│       └── dispatcher.py        # Routes SP list changes to the correct service pipeline
│
├── requirements.txt
├── Dockerfile
├── railway.toml
└── .env.example
```

---

## Core Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` | Web framework |
| `uvicorn` | ASGI server |
| `httpx` | Async HTTP client (Graph API, Twilio API) |
| `sqlalchemy[asyncio]` | ORM + async SQLite |
| `aiosqlite` | Async SQLite driver |
| `pydantic-settings` | Environment variable parsing |
| `python-multipart` | Twilio webhook form parsing |

---

## Startup Sequence

1. **Load and validate** all environment variables (fail fast if missing)
2. **Initialize SQLite** database and run migrations (create tables if not exist)
3. **Acquire Graph API token** — exit with error if credentials are bad
4. **Resolve and cache SharePoint site ID** for `UCSHBulletinBoard`
5. **Verify SharePoint access** by reading one Staff Directory item
6. **Register/renew SharePoint webhook subscriptions** for all 4 lists:
   - Leave Requests
   - Overtime Requests
   - CarryOver Payout
   - Company Holidays (to detect holiday changes)
7. **Start background task**: subscription renewal checker (runs daily)
8. **Log success**, begin serving requests

---

## SharePoint Webhook Flow

### Subscription Setup

For each SharePoint list, the server creates a webhook subscription via Graph API:

```
POST /sites/{siteId}/lists/{listId}/subscriptions
{
  "changeType": "updated,created",
  "notificationUrl": "{BASE_URL}/api/webhooks/sharepoint",
  "expirationDateTime": "now + 29 days",
  "clientState": "{random secret for validation}"
}
```

On creation, Graph sends a validation request with a `validationToken` query param. The server echoes it back with `200 text/plain`.

### Change Notification Processing

When SharePoint detects a change, it POSTs to the server:

1. Server responds `202 Accepted` immediately (Graph requires response within 30 seconds)
2. Server enqueues an async task to process the notification
3. Task queries the list's delta endpoint using the stored change token to get actual changed items
4. For each changed item, check the processing log for idempotency
5. Dispatch to the appropriate service pipeline based on list ID + change type

### Subscription Renewal

- Background task runs every 24 hours
- Queries local DB for subscriptions expiring within 5 days
- Renews each via PATCH to extend expiration by 29 days
- On startup, also checks and renews any stale subscriptions

---

## Request Processing Pipelines

### Leave Requests — Item Created

Triggered when MS Forms creates a new item in the Leave Requests list.

Three parallel async tasks fire (mirroring the original flows 02, 03, 08):

**Task A: Auto-Calculate Days (02)**
1. Look up employee in Staff Directory → get Location → map to Province
2. Fetch Company Holidays for that province + half-friday season dates
3. If partial day: run auto-rejection checks (holiday conflict, half-friday hour limit)
4. If standard leave: calculate business days (weekend/holiday/half-friday logic)
5. Update Leave Request item with calculated `Days`

**Task B: Auto-Assign Manager (03)**
1. Look up employee in Staff Directory → get Supervisor
2. Look up manager in Staff Directory
3. Update Leave Request with Manager, Managertxt, AllManagers, StaffLocation, StaffDepartment

**Task C: Bereavement/Jury Duty Alert (08)**
1. If LeaveType is Bereavement or Jury Duty → send alert email to admins
2. Otherwise → no action

**After Task B completes** (manager assigned), the approval pipeline begins.

### Leave Requests — Item Modified (Approval Pipeline)

The manager assignment from Task B triggers a "modified" webhook. The server:

1. Pre-check: Manager assigned? Status == Pending? ApproveProcessedFlag == Not Processed?
2. Look up employee and manager from Staff Directory
3. Acquire SystemCheck lock (async poll, max 60 iterations, 30s delay)
4. Send approval email with HMAC-signed approve/reject links
5. Wait for manager response (via email link click or SMS)

**On manager response (approve/reject endpoint hit):**

- **Approved:**
  1. Update Leave Request: Status=Approved, ApproveProcessedFlag=Processed, ApprovedDate=today
  2. Send approval confirmation email to employee
  3. If hourly staff → done (no balance adjustment)
  4. Deduct balance by leave type (Vacation/Sick → different starting balance)
  5. Set SystemCheck=Editing
  6. Run balance cascading loop (current year or next year sequence)
  7. Recalculate Request Allow Date for this employee
  8. Set SystemCheck=Ready
  9. Send balance update email
  10. Log new balances to Leave Request

- **Rejected:**
  1. Update Leave Request: Status=Rejected, ApproveProcessedFlag=Processed
  2. Send rejection email to employee

### Overtime Requests — Item Created

**Step 1: Auto-Assign Manager (02)**
1. Look up employee → get Supervisor → update Overtime Request with Manager

**Step 2: Approval Pipeline (03)** — triggered by manager assignment modification
1. Pre-check: Status == Pending? Manager assigned?
2. Look up employee, map Location → Province
3. Holiday check: if overtime date is a company holiday → auto-reject
4. Half-friday detection: flag if overtime date is a half-day Friday
5. Send approval email to manager (modified subject if half-friday)

**On response:**

- **Approved:**
  1. Calculate new overtime balance: `CurrentOvertimeBalance + (Hours / 8)`
  2. If hourly staff → simplified email, no balance update
  3. Update Staff Directory balance
  4. Update Overtime Request: Status=Approved, ApprovedDate=today
  5. Run vacation offset logic (if vacation negative and overtime now positive)
  6. Recalculate Request Allow Date for this employee
  7. Send approval email with updated balances

- **Rejected:**
  1. Send rejection email
  2. Update Overtime Request: Status=Rejected

### CarryOver/Payout Requests — Item Created

**Step 1: Auto-Assign Manager (02)**
1. Look up employee by email → get Supervisor, employee ID
2. Look up manager → get manager ID
3. Update request with Manager, Managertxt, EmployeeID, ManagerID

**Step 2: Approval Pipeline (03)** — triggered by manager assignment modification
1. Pre-check: Managertxt set? SystemState == Not Processed?
2. Set SystemState=Processing
3. Look up employee and manager by ID
4. Pre-validate:
   - Compute NewVacation = CurrentVacation - Days
   - If Payout: check NewPayout <= 5 cap
   - If NewVacation < 0: auto-reject
5. Send confirmation email to employee
6. Send approval email to manager (CC mandyl@ucsh.com)

**On response:**

- **Approved:**
  1. Acquire SystemCheck lock
  2. Re-read balances (may have changed since submission)
  3. Re-validate: if NewVacation < 0 → system override reject
  4. Set SystemCheck=Editing
  5. Apply balance transfer (Vacation → CarryOver or Vacation → Payout)
  6. Set SystemCheck=Ready
  7. Recalculate Request Allow Date for this employee
  8. Update request: Status=Approved, SystemState=Processed, NewBalance snapshot
  9. Send approval email

- **Rejected:**
  1. Update request: Status=Rejected, SystemState=Processed
  2. Send rejection email

---

## Request Allow Date — Integrated Recalculation

Called after any balance change (leave approval, overtime approval, carryover/payout approval).

**Input:** Employee's current `CurrentVacationBalance` and `CarryOver` values (freshly read).

**Logic (first match wins):**

| Condition | Request Allow Date |
|-----------|--------------------|
| Vacation == 0 AND CarryOver != 0 | Next year, March 31 |
| CarryOver == 0 AND Vacation != 0 | End of current year (Dec 31) |
| Vacation == 0 AND CarryOver == 0 | End of next year (Dec 31) |
| Vacation != 0 AND CarryOver != 0 | No change |

**Update:** Patch employee's `RequestAllowDate` in Staff Directory (only if value differs from current).

---

## Approval Link Format

### Generation

```
/api/{request_type}/approve/{request_id}?token={hmac}&mgr={manager_id}
/api/{request_type}/reject/{request_id}?token={hmac}&mgr={manager_id}
```

HMAC payload: `{request_type}:{request_id}:{action}:{manager_id}:{expiry_timestamp}`
Signed with: `APPROVAL_LINK_SECRET` using HMAC-SHA256

### Validation

1. Parse token from query string
2. Recompute HMAC from URL components
3. Compare (constant-time) with provided token
4. Check expiry timestamp hasn't passed
5. Verify manager_id matches the assigned manager (or is an authorized admin)

---

## SMS Approval Flow

### Inbound Endpoint

```
POST /api/twilio/sms
Content-Type: application/x-www-form-urlencoded
```

### Processing

1. Validate Twilio webhook signature (`X-Twilio-Signature`)
2. Parse `Body` and `From` fields
3. Extract decision (approve/reject) and request ID from body text
4. Match `From` (last 10 digits) against Staff Directory `CellNumber`
5. Authorization check: sender is assigned manager or authorized admin (Jay Puzon, Mandy Leong, Dave Powell)
6. If authorized → process approval/rejection (same logic as email link)
7. Reply SMS with confirmation

---

## Local Database Schema

### `webhook_subscriptions`

| Column | Type | Purpose |
|--------|------|---------|
| id | TEXT PK | Subscription ID from Graph API |
| list_id | TEXT | SharePoint list GUID |
| expiration | DATETIME | When the subscription expires |
| client_state | TEXT | Secret for validating incoming notifications |
| created_at | DATETIME | When created |

### `change_tokens`

| Column | Type | Purpose |
|--------|------|---------|
| list_id | TEXT PK | SharePoint list GUID |
| token | TEXT | Latest delta change token |
| updated_at | DATETIME | Last update time |

### `processing_log`

| Column | Type | Purpose |
|--------|------|---------|
| id | INTEGER PK | Auto-increment |
| list_id | TEXT | SharePoint list GUID |
| item_id | TEXT | SharePoint item ID |
| action | TEXT | What was done (e.g., "auto_calc_days", "approval_sent") |
| processed_at | DATETIME | When processed |

Composite unique index on `(list_id, item_id, action)` for idempotency.

---

## Error Handling Strategy

| Scenario | Approach |
|----------|----------|
| Graph API token expired mid-request | Auto-refresh and retry once |
| SharePoint item not found | Log warning, skip processing |
| SystemCheck stuck on "Editing" (60 iterations exhausted) | Log error, send alert email to admins, do not process |
| Twilio webhook signature invalid | Return 403, log attempt |
| HMAC approval link invalid/expired | Return 400 with user-friendly message |
| SharePoint webhook subscription creation fails | Retry on startup, log error |
| Duplicate webhook notification | Idempotency check via processing_log, skip if already processed |

---

## Deployment

### Railway Configuration

```toml
# railway.toml
[build]
builder = "dockerfile"

[deploy]
healthcheckPath = "/health"
healthcheckTimeout = 30
restartPolicyType = "always"
```

### Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

SQLite database file stored at `/app/data/gone_fishing.db` (Railway persistent volume recommended for durability across deploys).
