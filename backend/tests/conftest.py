import os
import sys

# Make `import app` resolve and keep cwd-relative paths (the SQLite db at
# app/data/) under backend/ regardless of where pytest is invoked from.
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)
os.chdir(_BACKEND)

# Dummy settings so app.config.Settings() instantiates without real secrets.
# None of these logic tests make real Graph/Twilio calls; APPROVAL_LINK_SECRET
# just needs to be stable so HMAC signing/validation round-trips.
os.environ.setdefault("AZURE_TENANT_ID", "test-tenant")
os.environ.setdefault("AZURE_CLIENT_ID", "test-client")
os.environ.setdefault("AZURE_CLIENT_SECRET", "test-secret")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550000")
os.environ.setdefault("APPROVAL_LINK_SECRET", "test-approval-secret")
os.environ.setdefault("BASE_URL", "https://test.example.com")
os.environ.setdefault("SMTP2GO_API_KEY", "test-smtp-key")
