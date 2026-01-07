import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .authenctication import AuthUser, require_auth_user
from .config import get_settings
from .db import db_execute, db_fetch, db_fetchrow
from .tenants import get_tenant_id, is_tenant_admin

settings = get_settings()
logger = logging.getLogger(__name__)

channels_router = APIRouter(prefix="/tenants/{tenant_id}/channels", tags=["channels"])


# --- Data class ---


@dataclass
class Channel:
    id: str
    name: str
    tenant_id: str
    description: str | None = None
    is_open: bool = True
    is_private: bool = False
    parent_id: str | None = None
    updated_at: str = field(default_factory=lambda: str(datetime.now(timezone.utc)))
    updated_by: str | None = None


# --- API Models ---


class ChannelBase(BaseModel):
    id: str = Field(..., pattern="^[a-z0-9._-]+$", description="Unique channel identifier.")
    name: str
    description: str | None = None
    is_open: bool = True
    is_private: bool = False
    parent_id: str | None = None


class ChannelCreate(ChannelBase):
    pass


class ChannelUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_open: bool | None = None
    is_private: bool | None = None
    parent_id: str | None = None


class ChannelRead(ChannelBase):
    id: str
    tenant_id: str
    updated_at: str = ""
    updated_by: str = ""


# --- Init function ---


async def channels_init() -> None:
    create_table = """
        CREATE TABLE IF NOT EXISTS channels (
            id text PRIMARY KEY,
            data jsonb NOT NULL
        )
    """
    await db_execute(create_table)
    await db_execute("CREATE INDEX IF NOT EXISTS idx_tenant_id ON channels ((lower(data->>'tenant_id')))")
    await db_execute("CREATE INDEX IF NOT EXISTS idx_parent_id ON channels ((lower(data->>'parent_id')))")

    row = await db_fetchrow("SELECT data FROM channels WHERE id='heartbeat'")
    if not row:  # Create a heartbeat channel
        data = Channel(
            id="heartbeat",
            name="Heartbeat channel",
            tenant_id=settings.DEFAULT_TENANT,
            description="Sends heartbeats once a minute",
            updated_by="Init script",
        )
        await db_execute("INSERT INTO channels (id, data) VALUES ($1, $2)", data.id, asdict(data))
    return


# --- Channel dependency function ---


async def get_channel_id(channel_id: str):
    channel = await db_fetchrow("SELECT data FROM channels WHERE id=$1", channel_id)
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )
    return channel_id


# --- API functions ---


@channels_router.get(
    "",
    response_model=list[ChannelRead],
    status_code=status.HTTP_200_OK,
    response_description="Channel list",
)
async def list_channels(
    include_private: bool = Query(default=False, description="Also include private channels"),
    tenant: str = Depends(get_tenant_id),
    user: AuthUser = Depends(require_auth_user),
):
    """
    Get all channels for a tenant
    """
    # is_admin = user_is_tenant_admin(user, tenant)
    # if include_private and not is_admin:
    #     raise HTTPException(
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         detail="Only tenant admins can view private channels.",
    #     )
    channels = await db_fetch("SELECT data FROM channels WHERE data->>'tenant_id' = $1", tenant)
    return [d["data"] for d in channels] if channels else []


@channels_router.post(
    "", response_model=ChannelRead, status_code=status.HTTP_201_CREATED, response_description="Channel created"
)
async def create_channel(
    payload: ChannelCreate, tenant: str = Depends(get_tenant_id), user: AuthUser = Depends(require_auth_user)
):
    """
    Create a new channel
    """
    if not await is_tenant_admin(tenant, user.preferred_username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required.",
        )
    channel = await db_fetchrow("SELECT data FROM channels WHERE id=$1", payload.id)
    if channel:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Channel '{payload.id}' already exists.",
        )
    if payload.parent_id:
        parent_channel = await db_fetchrow("SELECT data FROM channels WHERE id=$1", payload.parent_id)
        if not parent_channel:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Parent channel not found.",
            )

    data = payload.model_dump()
    data.update({"tenant_id": tenant, "updated_at": str(datetime.now(timezone.utc)), "updated_by": user.subject})
    await db_execute("INSERT INTO channels (id, data) VALUES ($1, $2)", payload.id, data)
    # channel = await db_fetchrow("SELECT data FROM channels WHERE id=$1", payload.id)
    # return channel["data"]
    return data


# @channels_router.patch("/{channel_key}", response_model=ChannelRead)
# async def update_channel(
#     channel_key: str,
#     payload: ChannelUpdate,
#     tenant: Tenant = Depends(get_tenant),
#     current_user: TokenClaims = Depends(get_current_user),
#     service: ChannelService = Depends(get_channel_service),
# ) -> ChannelRead:
#     if not user_is_tenant_admin(current_user, tenant):
#         raise HTTPException(
#             status_code=status.HTTP_403_FORBIDDEN,
#             detail="Admin privileges required.",
#         )
#     return await service.update_channel(tenant, channel_key, payload)


@channels_router.delete(
    "/{channel_id}", response_model=None, status_code=status.HTTP_204_NO_CONTENT, response_description="Channel deleted"
)
async def delete_channel(
    channel_id: str, tenant: str = Depends(get_tenant_id), user: AuthUser = Depends(require_auth_user)
):
    """
    Delete channel
    """
    # if not user_is_tenant_admin(current_user, tenant):
    #     raise HTTPException(
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         detail="Admin privileges required.",
    #     )
    channel = await db_fetchrow("SELECT data FROM channels WHERE id=$1", channel_id)
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )
    await db_execute("DELETE FROM channels WHERE id=$1", channel_id)
    return
