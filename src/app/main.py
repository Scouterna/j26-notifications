# from pathlib import Path
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .channels import channels_init, channels_router
from .config import get_settings
from .db import close_db_connection, connect_to_db
from .exeptions import J26NotificationError
from .firebase import firebase_init
from .heartbeats import heartbeats_init
from .notifications import notifications_init, notifications_router
from .subscriptions import subscriptions_init, subscriptions_router
from .tenants import tenants_init, tenants_router

# from .info_api import router as info_router

# --- Create instrumentor, settings and logger objects ---
instrumentator = Instrumentator()
settings = get_settings()
logger = logging.getLogger(__name__)


# --- Lifespan event handler to create DB indexes on startup ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages application startup and shutdown events.
    Creates database indexes on startup and start the heartbeats
    """
    await connect_to_db()
    await tenants_init()
    await channels_init()
    await subscriptions_init()
    await notifications_init()
    await firebase_init()
    await heartbeats_init()

    try:

        yield  # Run FastAPI!

    except Exception as e:
        if isinstance(e, J26NotificationError):
            # logging.fatal("J26NotificationError error: %s", str(e), exc_info=False)
            pass
        else:
            # logg§ing.fatal("Unexpected fatal error: %s", str(e), exc_info=True)
            pass

    finally:  # Make sure DB connection is closed on exit
        await close_db_connection()


# --- Initialize FastAPI app with the lifespan manager and session middleware ---
app = FastAPI(
    title="j26-notifications-api",
    version="0.1.0",
    lifespan=lifespan,
    root_path=settings.ROOT_PATH,
    openapi_url=f"{settings.API_PREFIX}/openapi.json",
    docs_url=None,
)

app.add_middleware(SessionMiddleware, secret_key=settings.SESSION_SECRET_KEY)
# Allow credentials and permit all origins via a regex (wildcard + credentials is not allowed)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Add metrics API ---
instrumentator.instrument(app)  # Adds Prometheus middleware during initialization
instrumentator.expose(app)  # Registers /metrics endpoint before other catch-all routes


# --- Include the API routers ---
app.include_router(tenants_router, prefix=settings.API_PREFIX)
app.include_router(channels_router, prefix=settings.API_PREFIX)
app.include_router(subscriptions_router, prefix=settings.API_PREFIX)
app.include_router(notifications_router, prefix=settings.API_PREFIX)

if not os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/namespace"):  # We are running in dev
    from .auth_api import auth_router

    app.include_router(auth_router, include_in_schema=False)  # We need the '/auth' API


# --- Custom Swagger UI route with configurable root path ---
@app.get(f"{settings.API_PREFIX}/docs", include_in_schema=False)
async def custom_swagger_ui_html(request: Request):
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    root_path = forwarded_prefix or settings.ROOT_PATH.rstrip("/")
    openapi_url = (
        f"{root_path}{settings.API_PREFIX}/openapi.json"
        if root_path
        else f"{settings.API_PREFIX}/openapi.json"
    )
    return get_swagger_ui_html(
        openapi_url=openapi_url,
        title="j26-notifications-api - Swagger UI",
    )


# --- Add a root endpoint for basic API health check ---
@app.get("/", include_in_schema=False)
def read_root():
    return {"message": "FastAPI server is running"}
