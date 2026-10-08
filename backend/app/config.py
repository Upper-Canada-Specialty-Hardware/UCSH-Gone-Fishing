from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Microsoft Entra ID
    AZURE_TENANT_ID: str
    AZURE_CLIENT_ID: str
    AZURE_CLIENT_SECRET: str

    # Twilio
    TWILIO_ACCOUNT_SID: str
    TWILIO_AUTH_TOKEN: str
    TWILIO_PHONE_NUMBER: str = "+16476977133"

    # Server
    APPROVAL_LINK_SECRET: str
    BASE_URL: str = "https://backend-gone-fishing-production.up.railway.app"

    # SharePoint
    SP_SITE_HOST: str = "ucshca.sharepoint.com"
    SP_SITE_PATH: str = "/sites/UCSHBulletinBoard"
    SP_LIST_STAFF_DIRECTORY: str = "ed4bba96-f035-4eee-a8ff-af71036034fe"
    SP_LIST_LEAVE_REQUESTS: str = "bd21037a-9c3c-4682-9aaa-948095e16aec"
    SP_LIST_OVERTIME_REQUESTS: str = "1ea6f753-5c84-450f-b266-707d73c71133"
    SP_LIST_CARRYOVER_PAYOUT: str = "bcfd8cc6-b29f-4d6f-a449-cb1d0a9251bb"
    SP_LIST_COMPANY_HOLIDAYS: str = "391e299d-c537-44c4-90c8-462e2ca2db5f"

    # Database — Railway injects DATABASE_URL; empty = SQLite fallback for local dev
    DATABASE_URL: str = ""

    # Processing toggle — when False, app is read-only (dashboards only)
    PROCESSING_ENABLED: bool = False

    # Storage backend per domain (SharePoint -> Postgres migration cutover flags).
    # "sharepoint" (default) keeps SharePoint as the source of truth; "postgres"
    # switches that domain's reads/writes to Postgres. Flipped per cutover PR once
    # that domain's Postgres implementation exists.
    STORAGE_HOLIDAYS: str = "sharepoint"
    STORAGE_EMPLOYEES: str = "sharepoint"
    STORAGE_REQUESTS: str = "sharepoint"

    # Dashboard
    DASHBOARD_FRONTEND_URL: str = ""

    # Public request page (#131)
    # Write SubmitterEmail and RequestSource on new request items. Off until
    # those columns exist on the three request lists: writing a column that is
    # not there makes SharePoint refuse the whole item.
    REQUEST_EMAIL_COLUMNS_ENABLED: bool = False

    # Email (SMTP2GO): the fallback mailer; always available.
    SMTP2GO_API_KEY: str
    SENDER_EMAIL: str = "HR@s2gms.com"

    # Email (UCSH mailer): internal HTTPS service over Microsoft High Volume
    # Email, used for staff (UCSH-domain) recipients once switched on. Off and
    # blank by default, so nothing changes until an operator fills these in.
    MAILER_ENABLED: bool = False                       # master switch for the UCSH mailer
    MAILER_URL: str = ""                               # base url, e.g. https://mailer.internal (no trailing /v1/send)
    MAILER_KEY: str = ""                               # static per-product bearer key from the mailer owner
    MAILER_FROM: str = ""                              # sender address; only needed when the key has several senders
    MAILER_FROM_NAME: str = "UCSH Out of Office"       # display name shown to staff recipients

    # Email (Clerk): transactional email for every non-UCSH recipient once
    # switched on. Off and blank by default. The endpoint is experimental.
    CLERK_EMAIL_ENABLED: bool = False                  # master switch for Clerk email
    CLERK_SECRET_KEY: str = ""                         # production secret key (sk_live_...)
    CLERK_API_URL: str = "https://api.clerk.com/v1"    # base url; /email is appended per call
    CLERK_FROM_EMAIL: str = ""                         # sender on the verified Clerk production domain
    CLERK_REPLY_TO: str = ""                           # optional reply-to, must be on the same domain

    # Which email domains count as internal (routed to the UCSH mailer). Comma
    # separated; must match the mailer's own ALLOWED_DOMAINS or it will refuse
    # recipients it considers external.
    INTERNAL_EMAIL_DOMAINS: str = "ucsh.com,ucaccess.com"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
