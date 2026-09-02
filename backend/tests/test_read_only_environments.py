"""A Railway environment that is not production can never process or send.

A PR environment is forked from production with the same variables and an
empty database. If it honoured PROCESSING_ENABLED=true it would re-send every
reminder and dashboard-link renewal and register webhook subscriptions of its
own against the shared SharePoint lists. The settings refuse that anywhere
but the environment named "production", so a preview is safe by construction.
"""

from app.config import PRODUCTION_ENVIRONMENT, Settings

_SECRETS = dict(
    AZURE_TENANT_ID="t", AZURE_CLIENT_ID="c", AZURE_CLIENT_SECRET="s",
    TWILIO_ACCOUNT_SID="a", TWILIO_AUTH_TOKEN="b",
    APPROVAL_LINK_SECRET="k", SMTP2GO_API_KEY="m",
)


def _settings(**overrides) -> Settings:
    """Settings built from explicit values only: no .env, no ambient variables."""
    return Settings(_env_file=None, **_SECRETS, **overrides)


def test_a_pr_environment_is_forced_read_only():
    s = _settings(RAILWAY_ENVIRONMENT_NAME="pr-122", PROCESSING_ENABLED=True)
    assert s.PROCESSING_ENABLED is False
    assert s.is_preview_environment is True


def test_production_keeps_the_flag_it_was_given():
    s = _settings(RAILWAY_ENVIRONMENT_NAME=PRODUCTION_ENVIRONMENT, PROCESSING_ENABLED=True)
    assert s.PROCESSING_ENABLED is True
    assert s.is_preview_environment is False


def test_local_runs_are_unaffected():
    # No Railway variable at all: whatever the .env says stands.
    s = _settings(RAILWAY_ENVIRONMENT_NAME="", PROCESSING_ENABLED=True)
    assert s.PROCESSING_ENABLED is True
    assert s.is_preview_environment is False
