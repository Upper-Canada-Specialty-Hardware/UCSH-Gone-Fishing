import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import init_db
from app.graph.auth import token_manager
from app.graph.sharepoint import sp_client
from app.services.concurrency import lock_manager
from app.tasks.subscription_manager import start_subscription_renewal_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    logger.info("Starting UCSH Gone Fishing server...")

    # 1. Initialize SQLite DB
    await init_db()
    logger.info("Database initialized")

    # 2. Connect to Graph API and SharePoint
    renewal_task = None
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

        from app.tasks.subscription_manager import register_all_subscriptions
        await register_all_subscriptions()

        renewal_task = start_subscription_renewal_task()
        logger.info("EmployeeLockManager ready")
    except Exception:
        logger.exception("Graph/SharePoint startup failed — app will serve but SP features are unavailable until restart")

    logger.info("Startup complete — serving requests")
    yield

    # --- Shutdown ---
    logger.info("Shutting down...")
    if renewal_task:
        renewal_task.cancel()
    await sp_client.close()
    logger.info("Shutdown complete")


app = FastAPI(title="UCSH Gone Fishing", lifespan=lifespan)

templates = Jinja2Templates(directory="app/templates")

# Register routers
from app.routes.health import router as health_router
from app.routes.forms import router as forms_router
from app.routes.approval import router as approval_router
from app.routes.webhooks import router as webhooks_router
from app.routes.twilio import router as twilio_router

app.include_router(health_router)
app.include_router(forms_router, prefix="/api/forms", tags=["forms"])
app.include_router(approval_router, prefix="/api", tags=["approval"])
app.include_router(webhooks_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(twilio_router, prefix="/api/twilio", tags=["twilio"])
