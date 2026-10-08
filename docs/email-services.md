# Email services

The backend can send each email through one of two services, chosen per
recipient by the recipient's domain. One send can touch both at once.

## What goes where

Every address in a send's `to` and `cc` is routed independently:

- **UCSH (internal) addresses** go to the **UCSH mailer**, an internal HTTPS
  service that relays mail over Microsoft High Volume Email. "Internal" means
  the domain is in `INTERNAL_EMAIL_DOMAINS` (default `ucsh.com,ucaccess.com`).
- **Every other address**, and every internal one while the mailer is switched
  off (or switched on but not yet configured), goes to **SMTP2GO**, exactly as
  before. With the mailer off, which is the default, every recipient goes to
  SMTP2GO and nothing about the old behaviour changes.

Mail is never dropped: if the mailer is enabled but missing its URL or key,
that is logged as an error and those recipients fall back to SMTP2GO.

Each HTTP call leaves one row in the `email_api_log` table, so the admin Email
Log tab shows every send whatever service handled it. The service is shown per
row (derived from the request URL, so no database column was added). The API
key and the HTML body are never stored, for either service.

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
   refused (`recipient_not_internal`); the app then sends them through SMTP2GO
   and logs a warning, so mail still arrives.
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

### (b) Removing SMTP2GO

SMTP2GO stays for every non-UCSH address until another service takes those
over. Do not remove the SMTP2GO code or the `SMTP2GO_API_KEY` setting before
then.
