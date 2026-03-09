# 08 - Authentication Spec

How the Railway server authenticates with Microsoft services and Twilio.

---

## Overview

The server uses two authentication mechanisms:

| Service | Auth Method | Purpose |
|---------|-----------|---------|
| Microsoft Graph API | OAuth 2.0 Client Credentials (Entra ID App Registration) | SharePoint lists, SharePoint files, sending email |
| Twilio | API Key + Auth Token | Sending SMS, receiving SMS webhooks |

There is **no user login** involved. The server authenticates as an application, not as a person.

---

## Microsoft Graph API Authentication

### Credentials (Environment Variables)

| Variable | Description | Source |
|----------|-------------|--------|
| `AZURE_TENANT_ID` | Microsoft Entra tenant ID | Entra ID admin provides (expect `3f51285e-5594-4af6-869b-fe72b32298fd`) |
| `AZURE_CLIENT_ID` | Application (client) ID from the App Registration | Entra ID admin provides |
| `AZURE_CLIENT_SECRET` | Client secret value from the App Registration | Entra ID admin provides |

### Token Acquisition — Client Credentials Flow

The server requests an access token from the Microsoft identity platform using the [OAuth 2.0 Client Credentials Grant](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow).

**Token endpoint:**
```
POST https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token
```

**Request body (form-encoded):**
```
client_id={AZURE_CLIENT_ID}
&client_secret={AZURE_CLIENT_SECRET}
&scope=https://graph.microsoft.com/.default
&grant_type=client_credentials
```

**Response (JSON):**
```json
{
  "access_token": "eyJ0eXAi...",
  "token_type": "Bearer",
  "expires_in": 3599
}
```

### Token Lifecycle

- Tokens expire after **1 hour** (3599 seconds)
- The server must cache the token and refresh it before expiry
- Recommended: request a new token when the cached token has less than 5 minutes remaining
- There is no refresh token in the client credentials flow — just request a new access token each time

### Using the Token

All Microsoft Graph API calls include the token in the Authorization header:

```
GET https://graph.microsoft.com/v1.0/sites/{siteId}/lists/{listId}/items
Authorization: Bearer {access_token}
Content-Type: application/json
```

### Permissions Granted

| Permission | What It Allows |
|-----------|---------------|
| `Sites.ReadWrite.All` | Full CRUD on all SharePoint list items and list schemas across the tenant |
| `Files.ReadWrite.All` | Read/write/create files in any SharePoint document library |
| `Mail.Send` | Send email as any user in the tenant (restricted to `hr@ucshca.onmicrosoft.com` by application access policy) |

---

## Microsoft Graph API — SharePoint Operations

### Site Identification

The SharePoint site URL is `https://ucshca.sharepoint.com/sites/UCSHBulletinBoard`.

To resolve the site ID (needed for all subsequent calls):
```
GET https://graph.microsoft.com/v1.0/sites/ucshca.sharepoint.com:/sites/UCSHBulletinBoard
```

This returns a site object with an `id` field. Cache this — it doesn't change.

### List Operations

**Read items (with filtering):**
```
GET https://graph.microsoft.com/v1.0/sites/{siteId}/lists/{listId}/items
    ?$expand=fields
    &$filter=fields/Title eq 'John Smith'
    &$top=5000
```

**Create item:**
```
POST https://graph.microsoft.com/v1.0/sites/{siteId}/lists/{listId}/items
Content-Type: application/json

{
  "fields": {
    "Title": "John Smith",
    "StartDate": "2026-03-15",
    "Status": "Pending"
  }
}
```

**Update item:**
```
PATCH https://graph.microsoft.com/v1.0/sites/{siteId}/lists/{listId}/items/{itemId}/fields
Content-Type: application/json

{
  "CurrentVacationBalance": 12.5,
  "SystemCheck": "Editing"
}
```

**Get single item by ID:**
```
GET https://graph.microsoft.com/v1.0/sites/{siteId}/lists/{listId}/items/{itemId}?$expand=fields
```

### SharePoint List GUIDs

These are used as `{listId}` in the API calls:

| List | GUID |
|------|------|
| Staff Directory | `ed4bba96-f035-4eee-a8ff-af71036034fe` |
| Leave Requests | `bd21037a-9c3c-4682-9aaa-948095e16aec` |
| Overtime Requests | `1ea6f753-5c84-450f-b266-707d73c71133` |
| CarryOver Payout | `bcfd8cc6-b29f-4d6f-a449-cb1d0a9251bb` |
| Company Holidays | `391e299d-c537-44c4-90c8-462e2ca2db5f` |

### Person/Group Fields

SharePoint Person or Group fields (like `Manager`, `SubmittedBy`) require special handling via Graph API:

**Reading:** The value is returned as a `lookupId` (integer) referencing the site's User Information List. Expand with `$select` to get display name and email.

**Writing:** Set the field using the `LookupId`:
```json
{
  "ManagerLookupId": 42
}
```

Or for Claims-based assignment (matching how Power Automate sets them), you may need to resolve the user first:
```
GET https://graph.microsoft.com/v1.0/sites/{siteId}/lists/User Information List/items
    ?$filter=fields/EMail eq 'manager@ucsh.com'
    &$expand=fields
```

### File Operations (for Reports)

**Upload a file:**
```
PUT https://graph.microsoft.com/v1.0/sites/{siteId}/drive/root:/Shared Documents/path/filename.csv:/content
Content-Type: text/csv

{file content}
```

**Download a file:**
```
GET https://graph.microsoft.com/v1.0/sites/{siteId}/drive/root:/Shared Documents/path/filename.xlsx:/content
```

---

## Microsoft Graph API — Email Operations

### Sending Email

```
POST https://graph.microsoft.com/v1.0/users/hr@ucshca.onmicrosoft.com/sendMail
Content-Type: application/json

{
  "message": {
    "subject": "Leave Request: Approved",
    "body": {
      "contentType": "HTML",
      "content": "<p>Your leave request has been approved...</p>"
    },
    "toRecipients": [
      {
        "emailAddress": {
          "address": "employee@ucsh.com"
        }
      }
    ],
    "ccRecipients": [
      {
        "emailAddress": {
          "address": "manager@ucsh.com"
        }
      }
    ]
  },
  "saveToSentItems": false
}
```

**Key points:**
- The URL path includes the sender mailbox: `/users/hr@ucshca.onmicrosoft.com/sendMail`
- HTML content is supported (all existing notification templates use HTML)
- Multiple To/CC recipients are supported as arrays
- `saveToSentItems: false` is recommended to avoid filling the HR mailbox Sent Items (high volume)
- Attachments are supported via the `attachments` array with base64-encoded content

### Sending Email with Attachments (for Reports)

```json
{
  "message": {
    "subject": "EOM Manager Report",
    "body": { "contentType": "HTML", "content": "..." },
    "toRecipients": [{ "emailAddress": { "address": "manager@ucsh.com" } }],
    "attachments": [
      {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": "Report.xlsx",
        "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "contentBytes": "{base64-encoded file content}"
      }
    ]
  }
}
```

### Approval Emails — No Built-In "Actionable Messages"

Power Automate used `SendMailWithOptions` which embeds approve/reject buttons directly in the email via Outlook Actionable Messages. Replicating this requires registering an Actionable Message provider with Microsoft, which is complex.

**Recommended alternative:** Include approve/reject **links** in the email body that point to the server's API endpoints:

```html
<p>
  <a href="https://{server}/api/leave/approve/{requestId}?token={hmac}">Approve</a>
  &nbsp;|&nbsp;
  <a href="https://{server}/api/leave/reject/{requestId}?token={hmac}">Reject</a>
</p>
```

The `token` parameter is an HMAC signature that authenticates the link so only the intended manager can use it (see Security Considerations below).

---

## Twilio Authentication

### Credentials (Environment Variables)

| Variable | Description |
|----------|-------------|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_PHONE_NUMBER` | `+16476977133` |

### Sending SMS

Using the Twilio REST API:
```
POST https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json
Authorization: Basic {base64(TWILIO_ACCOUNT_SID:TWILIO_AUTH_TOKEN)}

From=+16476977133
&To=+1{managerCellNumber}
&Body=Leave Request #{itemId} for {employeeName}. Reply "Approve {itemId}" or "Reject {itemId}"
```

### Receiving SMS (Webhook)

Twilio sends incoming SMS to a webhook URL configured in the Twilio console. The server must expose a public endpoint:

```
POST https://{server}/api/twilio/sms
```

Twilio POSTs form-encoded data including:
- `From` — sender's phone number
- `Body` — the SMS text content
- `To` — the Twilio number that received the message

### Twilio Webhook Validation

Twilio signs every webhook request. The server should validate the `X-Twilio-Signature` header using the Auth Token to confirm the request actually came from Twilio and wasn't spoofed. Most Twilio SDKs provide a `validateRequest` helper for this.

---

## Security Considerations

### Environment Variable Storage

All secrets are stored as Railway environment variables, never in source code:

| Variable | Sensitivity |
|----------|------------|
| `AZURE_TENANT_ID` | Low (publicly discoverable) |
| `AZURE_CLIENT_ID` | Low (not secret by itself) |
| `AZURE_CLIENT_SECRET` | **High** — rotate on schedule, never log |
| `TWILIO_ACCOUNT_SID` | Medium |
| `TWILIO_AUTH_TOKEN` | **High** — never log |

### Email Approval Link Security

Since approval emails contain links instead of embedded buttons, each link must include a tamper-proof token:

1. Generate an HMAC-SHA256 of the request ID + action (approve/reject) + manager ID, using a server-side secret key
2. Include this as a `token` query parameter in the link
3. When the link is clicked, the server validates the HMAC before processing
4. Optionally add an expiration timestamp to the HMAC payload

This prevents:
- Unauthorized users from approving/rejecting by guessing URLs
- Tampering with the request ID or action in the URL

### SMS Approval Security

The existing SMS flow validates the sender by:
1. Matching the incoming phone number (`From`) against the `CellNumber` field in the Staff Directory
2. Verifying the matched person is either the assigned manager or an authorized admin (Jay Puzon, Mandy Leong, Dave Powell)

The server must replicate this validation. Additionally, validate the Twilio webhook signature to prevent spoofed requests.

### Token Caching

- Cache Graph API access tokens in memory
- Do not persist tokens to disk or database
- Clear cached tokens on server restart

### Client Secret Rotation

The client secret has an expiration date (set during creation in Entra ID). The admin must rotate the secret before it expires:

1. Admin creates a new secret in Entra ID (the old one still works until it expires)
2. Admin provides the new secret value to the development team
3. Development team updates the `AZURE_CLIENT_SECRET` environment variable in Railway
4. Railway restarts the server, which picks up the new secret
5. After confirming the new secret works, the admin can optionally delete the old secret

---

## Startup Sequence

When the server starts, it should:

1. Validate all required environment variables are present (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`)
2. Request an initial Graph API access token — if this fails, the server should exit with a clear error (bad credentials, expired secret, etc.)
3. Resolve and cache the SharePoint site ID for `UCSHBulletinBoard`
4. Verify access by reading a single item from the Staff Directory list — if this fails, permissions may not be granted
5. Log success and begin normal operation

This fail-fast approach ensures misconfiguration is caught immediately rather than on the first user request.
