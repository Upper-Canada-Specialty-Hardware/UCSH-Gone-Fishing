# Email services

The backend can send each email through one of three services, chosen per
recipient by the recipient's domain. One send can touch more than one service
at once.

## What goes where

Every address in a send's `to` and `cc` is routed independently:

- **UCSH (internal) addresses** go to the **UCSH mailer**, an internal HTTPS
  service that relays mail over Microsoft High Volume Email. "Internal" means
  the domain is in `INTERNAL_EMAIL_DOMAINS` (default `ucsh.com,ucaccess.com`).
- **Every other address** goes to **Clerk** transactional email.
- **Any recipient whose service is switched off** (or switched on but not yet
  configured) stays on **SMTP2GO**, exactly as before. With both new services
  off, which is the default, every recipient goes to SMTP2GO and nothing about
  the old behaviour changes.

Mail is never dropped: if a service is enabled but missing its URL, key or
sender, that is logged as an error and those recipients fall back to SMTP2GO.

Each HTTP call leaves one row in the `email_api_log` table, so the admin Email
Log tab shows every send whatever service handled it. The service is shown per
row (derived from the request URL, so no database column was added). The API
key and the HTML body are never stored, for any service.

## Settings

All default to off/blank, so adding the code changes nothing until an operator
fills these in.

### UCSH mailer

| Setting | Default | Meaning |
| --- | --- | --- |
| `MAILER_ENABLED` | `False` | Master switch for the UCSH mailer. |
| `MAILER_URL` | `""` | Base URL of the mailer, e.g. `https://mailer.internal` (no trailing `/v1/send`). |
| `MAILER_KEY` | `""` | Static per-product bearer key from the mailer's owner. |
| `MAILER_FROM` | `""` | Sender address. Only needed when the key has several senders; a single-sender key supplies its own. |
| `MAILER_FROM_NAME` | `UCSH Out of Office` | Display name staff recipients see. |
| `INTERNAL_EMAIL_DOMAINS` | `ucsh.com,ucaccess.com` | Comma-separated domains routed to the mailer. Must match the mailer's own `ALLOWED_DOMAINS`. |

### Clerk

| Setting | Default | Meaning |
| --- | --- | --- |
| `CLERK_EMAIL_ENABLED` | `False` | Master switch for Clerk email. |
| `CLERK_SECRET_KEY` | `""` | Production secret key (`sk_live_...`). |
| `CLERK_API_URL` | `https://api.clerk.com/v1` | Base URL; `/email` is appended per call. |
| `CLERK_FROM_EMAIL` | `""` | Sender on the verified Clerk production domain. |
| `CLERK_REPLY_TO` | `""` | Optional reply-to, must be on the same domain. |

### SMTP2GO

Unchanged. `SMTP2GO_API_KEY` and `SENDER_EMAIL` stay exactly as they are.

## Setup steps

### (a) UCSH mailer

1. Ask the mailer's owner for a product key. They generate it with
   `npm run keygen -- out-of-office <sender@ucsh.com>` in the `ucsh-mailer`
   repo.
2. Note the key's sender address (the `<sender@ucsh.com>` it was made for).
3. Set `MAILER_URL`, `MAILER_KEY`, and `MAILER_FROM` (the sender address from
   step 2).
4. Confirm `INTERNAL_EMAIL_DOMAINS` matches the mailer's `ALLOWED_DOMAINS`, or
   the mailer will refuse recipients it considers external.
5. Turn on `MAILER_ENABLED`.
6. Make the app email a UCSH address you can read (for example, submit a
   request page code to your own address once #131 is live, or approve a test
   request), then check the admin Email Log tab: the new row should show the
   mailer and an accepted answer.

### (b) Clerk

1. Create the Clerk application.
2. Add and verify the production sending domain: Clerk Dashboard, Domains, add
   the DNS records it lists and wait for verification.
3. Copy the production secret key (`sk_live_...`) into `CLERK_SECRET_KEY`.
4. Set `CLERK_FROM_EMAIL` to an address on that verified domain.
5. Turn on `CLERK_EMAIL_ENABLED`.
6. Make the app email a personal (non-UCSH) address you can read (a request
   page code is the simplest), then check the admin Email Log tab: the row
   should show Clerk and an accepted answer.

Note: Clerk's email endpoint is experimental and the endpoint path may change.

### (c) Removing SMTP2GO

Leave SMTP2GO in place until both the mailer and Clerk have run cleanly in
production for a while. Only then remove the SMTP2GO code and the
`SMTP2GO_API_KEY` setting. This change does not remove either.
