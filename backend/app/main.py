import asyncio
import logging
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.graph.auth import token_manager
from app.graph.sharepoint import sp_client
from app.services.concurrency import lock_manager
from app.tasks.subscription_manager import start_subscription_renewal_task
from app.tasks.carryover_reset import start_carryover_reset_task

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
    logger.info("Starting UCSH Gone Fishing server...")

    # 1. Run database migrations
    run_migrations()
    # Alembic's fileConfig resets root logger to WARN — restore our config
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    logger.info("Database migrations applied")

    # 2. Connect to Graph API and SharePoint
    renewal_task = None
    carryover_reset_task = None
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
    await sp_client.close()
    logger.info("Shutdown complete")


app = FastAPI(title="UCSH Gone Fishing", lifespan=lifespan)

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

templates = Jinja2Templates(directory="app/templates")

# Register routers
from app.routes.health import router as health_router
from app.routes.forms import router as forms_router
from app.routes.approval import router as approval_router
from app.routes.webhooks import router as webhooks_router
from app.routes.twilio import router as twilio_router
from app.routes.dashboard import router as dashboard_router

app.include_router(health_router)
app.include_router(forms_router, prefix="/api/forms", tags=["forms"])
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(webhooks_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(twilio_router, prefix="/api/twilio", tags=["twilio"])
app.include_router(approval_router, prefix="/api", tags=["approval"])
