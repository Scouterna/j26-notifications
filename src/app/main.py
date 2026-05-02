import copy
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator, metrics

from .config import get_settings
from .db import J26NotificationError, close_db_connection, connect_to_db, db_init_tables
from .firebase import firebase_init
from .notifications import notifications_router

# --- Create instrumentor, settings and logger objects ---
instrumentator = Instrumentator(
    excluded_handlers=["/metrics"],
    should_instrument_requests_inprogress=True,
    inprogress_name="http_requests_inprogress",
    inprogress_labels=True,
)
instrumentator.add(
    metrics.default(
        latency_lowr_buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0, float("inf")),
    )
)
settings = get_settings()
logger = logging.getLogger(__name__)


# --- Lifespan event handler to, e.g., create DB indexes on startup ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_db()
    await db_init_tables()
    await firebase_init()
    logger.info("j26-notifications started and is accepting connections!")

    try:
        yield

    except Exception as e:
        if isinstance(e, J26NotificationError):
            pass
        else:
            pass

    finally:
        await close_db_connection()


# --- Initialize FastAPI app with the lifespan manager and session middleware ---
app = FastAPI(
    title="j26-notifications",
    version="0.2.0",
    lifespan=lifespan,
    root_path=settings.ROOT_PATH,
    openapi_url=None,
    docs_url=None,
)


# Add "no-cache" to all responses
@app.middleware("http")
async def no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# --- Add metrics API ---
instrumentator.instrument(app)
instrumentator.expose(app)

# --- Include the API routers ---
app.include_router(notifications_router, prefix=settings.API_PREFIX)

# Running locally
if not os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/namespace"):
    from starlette.middleware.sessions import SessionMiddleware

    from .auth_api import auth_router

    app.add_middleware(SessionMiddleware, secret_key="dev-only-static-secret")
    app.include_router(auth_router, include_in_schema=False)  # We need the '/auth' API locally


# --- Custom Swagger UI route with configurable root path ---
@app.get(f"{settings.API_PREFIX}/docs", include_in_schema=False)
async def custom_swagger_ui_html(request: Request):
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    root_path = forwarded_prefix or settings.ROOT_PATH.rstrip("/")
    openapi_url = (
        f"{root_path}{settings.API_PREFIX}/openapi.json" if root_path else f"{settings.API_PREFIX}/openapi.json"
    )
    return get_swagger_ui_html(
        openapi_url=openapi_url,
        title="j26-notifications-api - Swagger UI",
    )


# --- Custom OpenAPI endpoint to include a root-path server for Swagger "Try it out" ---
@app.get(f"{settings.API_PREFIX}/openapi.json", include_in_schema=False)
async def custom_openapi(request: Request):
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    root_path = forwarded_prefix or settings.ROOT_PATH.rstrip("/")

    if app.openapi_schema is None:
        app.openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )

    schema = copy.deepcopy(app.openapi_schema)
    if root_path:
        schema["servers"] = [{"url": root_path}]
    else:
        schema.pop("servers", None)

    return JSONResponse(schema)


# --- Add a root endpoint for basic API health check ---
@app.get("/", include_in_schema=False)
def read_root():
    return {"message": "FastAPI server is running"}
