import asyncio
import logging
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.graph.auth import token_manager
from app.graph.sharepoint import sp_client
from app.tasks.subscription_manager import start_subscription_renewal_task
from app.tasks.carryover_reset import start_carryover_reset_task
from app.tasks.reminders import start_reminder_task
from app.tasks.setup_nudges import start_setup_nudge_task
from app.tasks.held_request_reminders import start_held_request_reminder_task
from app.tasks.sync_tasks import start_shadow_sync_task, start_sharepoint_check_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run_migrations():
    """Run Alembic migrations (sync — called before async event loop)."""
    cfg = AlembicConfig("alembic.ini")
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    logger.info("Starting UCSH Out of Office server...")

    # 1. Run database migrations
    run_migrations()
    # Alembic's fileConfig resets root logger to WARN — restore our config
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    logger.info("Database migrations applied")

    # Live request feed: starts on the database alone, so it works even if the
    # Graph/SharePoint startup below fails. It listens on Postgres (NOTIFY) or
    # polls on SQLite, and fans request events out to open dashboards (#328).
    from app.services.event_bus import event_bus
    await event_bus.start()

    # 2. Connect to Graph API and SharePoint
    renewal_task = None
    carryover_reset_task = None
    reminder_task = None
    setup_nudge_task = None
    held_request_task = None
    shadow_sync_task = None
    sharepoint_check_task = None
    try:
        await token_manager.get_token()
        logger.info("Graph API token acquired")

        await sp_client.resolve_site_id()
        logger.info(f"SharePoint site ID resolved: {sp_client.site_id}")

        items = await sp_client.get_list_items(
            settings.SP_LIST_STAFF_DIRECTORY, top=1, select=["Title"]
        )
        if items:
            logger.info(f"SharePoint access verified — read: {items[0].get('fields', {}).get('Title', '?')}")
        else:
            logger.warning("SharePoint access check returned no items")

        renewal_task = start_subscription_renewal_task()
        carryover_reset_task = start_carryover_reset_task()
        reminder_task = start_reminder_task()
        setup_nudge_task = start_setup_nudge_task()
        held_request_task = start_held_request_reminder_task()  # request page: new hire reminders
        # Postgres migration: shadow-sync SharePoint into Postgres every 5 minutes
        # before the switch, and sweep SharePoint for stragglers after it (#334, #336).
        shadow_sync_task = start_shadow_sync_task()
        sharepoint_check_task = start_sharepoint_check_task()

        # Defer catch-up and subscription registration — both can be slow under
        # backlog/rate-limit conditions, and Graph webhook validation needs the
        # server to already be accepting HTTP. Running them after yield keeps
        # /health responsive within Railway's healthcheck window.
        async def _deferred_catch_up():
            try:
                from app.tasks.change_processor import catch_up_all_lists
                await catch_up_all_lists()
            except Exception:
                logger.exception("Deferred catch-up failed")

        async def _deferred_subscriptions():
            await asyncio.sleep(3)
            try:
                from app.tasks.subscription_manager import register_all_subscriptions
                await register_all_subscriptions()
            except Exception:
                logger.exception("Deferred subscription registration failed")

        asyncio.create_task(_deferred_catch_up())
        asyncio.create_task(_deferred_subscriptions())
        logger.info("EmployeeLockManager ready")
    except Exception:
        logger.exception("Graph/SharePoint startup failed — app will serve but SP features are unavailable until restart")

    logger.info("Startup complete — serving requests")
    yield

    # --- Shutdown ---
    logger.info("Shutting down...")
    if renewal_task:
        renewal_task.cancel()
    if carryover_reset_task:
        carryover_reset_task.cancel()
    if reminder_task:
        reminder_task.cancel()
    if setup_nudge_task:
        setup_nudge_task.cancel()
    if held_request_task:
        held_request_task.cancel()
    if shadow_sync_task:
        shadow_sync_task.cancel()
    if sharepoint_check_task:
        sharepoint_check_task.cancel()
    from app.services.event_bus import event_bus
    await event_bus.stop()                                   # stop the live feed listener/poller
    await sp_client.close()
    logger.info("Shutdown complete")


app = FastAPI(title="UCSH Out of Office", lifespan=lifespan)

# CORS — allow dashboard frontend
cors_origins = [o for o in [settings.DASHBOARD_FRONTEND_URL] if o]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.middleware("http")
async def add_app_version_header(request: Request, call_next):
    """Stamp every response with the running build, for the frontend auto-update (#331).

    Args:
        request: The incoming request.
        call_next: The next handler in the chain.

    Returns:
        The response, with an ``X-App-Version`` header added.
    """
    from app.routes.events import app_version
    response = await call_next(request)
    response.headers["X-App-Version"] = app_version()        # the page reloads when this changes
    return response

templates = Jinja2Templates(directory="app/templates")

# Register routers
from app.routes.health import router as health_router
from app.routes.forms import router as forms_router
from app.routes.approval import router as approval_router
from app.routes.webhooks import router as webhooks_router
from app.routes.twilio import router as twilio_router
from app.routes.dashboard import router as dashboard_router
from app.routes.admin_requests import router as admin_requests_router
from app.routes.admin_storage import router as admin_storage_router
from app.routes.email_api_log import router as email_api_log_router
from app.routes.intake import router as intake_router
from app.routes.self_service import router as self_service_router
from app.routes.events import router as events_router
from app.routes.admin_auth import public_router as admin_sign_in_router, router as admin_password_router
from app.routes.admin_legacy import router as admin_legacy_router
from app.services.admin_auth import admin_guard

# Every /api/dashboard/admin/* call needs an admin session (#228). The guard
# runs for each router at that prefix and checks only the /admin paths.
ADMIN_GUARD = [Depends(admin_guard)]

app.include_router(health_router)
# Public request page sign-in: email a code, check it. Unauthenticated by design.
app.include_router(intake_router, prefix="/api/intake", tags=["intake"])
app.include_router(forms_router, prefix="/api/forms", tags=["forms"])
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["dashboard"], dependencies=ADMIN_GUARD)
# The admin requests screen's new, delete, preview and export endpoints (#278),
# in their own module but behind the same admin password at the same prefix.
app.include_router(admin_requests_router, prefix="/api/dashboard", tags=["dashboard"], dependencies=ADMIN_GUARD)
# Where requests are stored, and the switch to Postgres (#335): same prefix and
# admin password as the other admin write endpoints.
app.include_router(admin_storage_router, prefix="/api/dashboard", tags=["dashboard"], dependencies=ADMIN_GUARD)
# Same prefix as the dashboard router so the admin email-log lookup sits next
# to the other /admin/* endpoints; kept in its own module to stay small.
app.include_router(email_api_log_router, prefix="/api/dashboard", tags=["dashboard"], dependencies=ADMIN_GUARD)
# An employee signed in on the request page submits a request (/me/requests/{type}).
app.include_router(self_service_router, prefix="/api/dashboard", tags=["dashboard"], dependencies=ADMIN_GUARD)
# The live event stream, the catch-up fallback, the per-request timeline and the
# build version (#328-#332). Same prefix; the admin timeline sits under /admin so
# the guard protects it, while the stream, catch-up and /me and /team timelines
# authenticate themselves (signed token or admin session).
app.include_router(events_router, prefix="/api/dashboard", tags=["dashboard"], dependencies=ADMIN_GUARD)
# The admin password: sign in (public) and change it (behind the guard).
app.include_router(admin_sign_in_router, prefix="/api/admin-auth", tags=["admin-auth"])
app.include_router(admin_password_router, prefix="/api/dashboard", tags=["dashboard"], dependencies=ADMIN_GUARD)
# The admin legacy data handler (#337): list and resolve reconciliation findings,
# behind the same admin password at the same prefix as the other /admin/* routers.
app.include_router(admin_legacy_router, prefix="/api/dashboard", tags=["dashboard"], dependencies=ADMIN_GUARD)
app.include_router(webhooks_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(twilio_router, prefix="/api/twilio", tags=["twilio"])
app.include_router(approval_router, prefix="/api", tags=["approval"])
