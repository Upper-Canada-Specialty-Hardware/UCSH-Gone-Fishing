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

    # Dashboard
    DASHBOARD_FRONTEND_URL: str = ""

    # Email (SMTP2GO)
    SMTP2GO_API_KEY: str = ""
    SENDER_EMAIL: str = "HR@s2gms.com"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
