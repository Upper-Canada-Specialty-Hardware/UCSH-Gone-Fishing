# Email services

The backend can send each email through one of three services, chosen per
recipient by the recipient's domain. One send can touch more than one service
at once.

## What goes where

Every address in a send's `to` and `cc` is routed independently:

- **UCSH (internal) addresses** go to the **UCSH mailer**, an internal HTTPS
  service that relays mail over Microsoft High Volume Email. "Internal" means
  the domain is in `INTERNAL_EMAIL_DOMAINS` (default `ucsh.com`, the only
  domain the mailer accepts).
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
| `INTERNAL_EMAIL_DOMAINS` | `ucsh.com` | Comma-separated domains routed to the mailer. Must match the mailer's own `ALLOWED_DOMAINS`. |
| `EMAIL_REPLY_TO` | `""` | Reply-To for emails that name no person to answer (auto-rejections, receipts). Blank = none. |

### Reply-To

The sending accounts have no inbox, so an email that someone may answer
carries a Reply-To. A manager's approval request replies to the employee;
an employee's approved or rejected email replies to the manager. Other
emails use `EMAIL_REPLY_TO` when it is set. The mailer only accepts an
internal Reply-To, so an outside one is left off the mailer's copy (SMTP2GO
sends it as a header either way).

### Clerk

| Setting | Default | Meaning |
| --- | --- | --- |
| `CLERK_EMAIL_ENABLED` | `False` | Master switch for Clerk email. |
| `CLERK_SECRET_KEY` | `""` | Production secret key (`sk_live_...`). A development key cannot send. |
| `CLERK_API_URL` | `https://api.clerk.com/v1` | Base URL; `/email` is appended per call. |
| `CLERK_FROM_EMAIL` | `""` | Sender on the verified Clerk production domain. |
| `CLERK_REPLY_TO` | `""` | Optional reply-to, must be on the same domain. |

### SMTP2GO

Unchanged. `SMTP2GO_API_KEY` and `SENDER_EMAIL` stay exactly as they are.

## Setup steps

### (a) UCSH mailer

The mailer (repo `ucsh-mailer`, live at
`https://ucsh-mailer-production.up.railway.app`, sending over HVE with OAuth)
issues one key per product. This app's key is made there under the product
name `gone-fishing`, bound to the shared sender (mailer issue #16); its
maintainer creates it with `npm run onboard -- gone-fishing --apply` and hands
over two secrets.

1. Set `MAILER_URL` and `MAILER_KEY` to the two secrets. Leave `MAILER_FROM`
   blank: the key has one sender and the mailer uses it.
2. Confirm `INTERNAL_EMAIL_DOMAINS` matches the mailer's `ALLOWED_DOMAINS`. If
   the app lists a domain the mailer does not accept, those addresses are
   refused (`recipient_not_internal`); the app then sends them the outside way
   (Clerk, or SMTP2GO) and logs a warning, so mail still arrives.
3. Turn on `MAILER_ENABLED` only once the mailer can send through HVE (a test
   send from the mailer reaches an inbox). A mailer that is on but failing
   makes staff emails fail; they fall back to SMTP2GO only while the mailer is
   off or not configured.
4. Make the app email a UCSH address you can read (a request page code to your
   own address is simplest, or approve a test request), then check the admin
   Email Log tab: the new row should show the mailer and an accepted answer.

The mailer's yes/no rule (a link opens a confirmation page and only a `POST`
records the answer, because link scanners open links) already holds here: the
emailed approve and reject links show a confirmation page and act only on its
`POST` (`routes/approval.py`).

### (b) Clerk

Clerk's email endpoint only sends from the instance's verified production
domain, so a development instance cannot be used.

1. In the Clerk dashboard, create the application's production instance (the
   instance dropdown, Production, Create default instance) with `ucsh.com` as
   its domain.
2. Developers, Domains lists five CNAME records (`clerk`, `accounts`,
   `clkmail`, `clk._domainkey`, `clk2._domainkey`). Whoever runs the
   `ucsh.com` DNS in Cloudflare adds each one with proxy status DNS only. They
   are new names: the MX, the SPF record and existing mail are untouched.
3. Wait until Clerk shows every record verified, the email records included.
4. Set `CLERK_SECRET_KEY` to the production secret key (`sk_live_...`, from
   Developers, API keys with the Production instance selected).
5. Set `CLERK_FROM_EMAIL` to an address on that domain, e.g.
   `notifications@ucsh.com`.
6. Turn on `CLERK_EMAIL_ENABLED` only after step 3. A Clerk that is on but
   refusing sends makes those emails fail; they fall back to SMTP2GO only
   while Clerk is off or not configured.
7. Make the app email a personal (non-UCSH) address you can read (a request
   page code is the simplest), then check the admin Email Log tab: the row
   should show Clerk and an accepted answer.

Note: Clerk's email endpoint is experimental and the endpoint path may change.

### (c) Removing SMTP2GO

Leave SMTP2GO in place until both the mailer and Clerk have run cleanly in
production for a while. Only then remove the SMTP2GO code and the
`SMTP2GO_API_KEY` setting. This change does not remove either.
