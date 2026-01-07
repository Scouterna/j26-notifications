import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .authenctication import AuthUser, require_auth_user
from .config import get_settings
from .db import db_execute, db_fetch, db_fetchrow

settings = get_settings()
logger = logging.getLogger(__name__)

tenants_router = APIRouter(prefix="/tenants", tags=["tenants"])


# --- Data class ---


@dataclass
class Tenant:
    id: str
    name: str
    description: str | None = None
    default_locale: str = "sv"
    settings: dict[str, str] = field(default_factory=dict)
    admin_roles: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: str(datetime.now(timezone.utc)))


# --- API Models ---


class TenantBase(BaseModel):
    id: str = Field(..., pattern="^[a-z0-9._-]+$", description="Unique tenant identifier.")
    name: str
    description: str
    default_locale: str
    admin_roles: list[str]


class TenantCreate(TenantBase):
    pass


class TenantUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    default_locale: str | None = None
    admin_roles: list[str] | None = None


class TenantRead(TenantBase):
    created_at: datetime


# --- Tenant dependency function ---


async def get_tenant_id(tenant_id: str):
    tenant = await db_fetchrow("SELECT data FROM tenants WHERE id=$1", tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found.",
        )
    return tenant_id


async def is_tenant_admin(tenant_id: str, user_id: str) -> bool:
    return True


# --- Init function ---


async def tenants_init() -> None:
    create_table = """
        CREATE TABLE IF NOT EXISTS tenants (
            id text PRIMARY KEY,
            data jsonb NOT NULL
        )
    """
    await db_execute(create_table)

    row = await db_fetchrow("SELECT data FROM tenants WHERE id=$1", settings.DEFAULT_TENANT)
    if not row:  # Need to create a default tenant
        default_tenant = Tenant(
            id=settings.DEFAULT_TENANT,
            name=settings.DEFAULT_TENANT_NAME,
            description="Default tenant seeded from configuration.",
        )
        await db_execute("INSERT INTO tenants (id, data) VALUES ($1, $2)", "jamboree26", asdict(default_tenant))
    return


# --- API functions ---


@tenants_router.get(
    "",
    response_model=list[TenantRead],
    status_code=status.HTTP_200_OK,
    response_description="Returns all available tenants",
)
async def list_tenants(user: AuthUser = Depends(require_auth_user)):
    """
    Return all list of all available tenants.
    """
    rows = await db_fetch("SELECT data FROM tenants")
    return [d["data"] for d in rows]  # type: ignore[reportOptionalIterable]


@tenants_router.get(
    "/{tenant_id}",
    response_model=TenantRead,
    status_code=status.HTTP_200_OK,
    response_description="Returns requested tenant",
)
async def get_tenant(tenant: str = Depends(get_tenant_id), user: AuthUser = Depends(require_auth_user)):
    """
    Return information about a specific tenant.
    """
    row = await db_fetchrow("SELECT data FROM tenants WHERE id=$1", tenant)
    return row["data"]  # type: ignore[reportOptionalSubscript]
