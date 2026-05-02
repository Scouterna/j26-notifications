import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from uuid import uuid1

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .authenctication import AuthUser, require_auth_user
from .config import get_settings
from .db import db_execute, db_fetch
from .firebase import firebase_send
from .subscriptions import get_subscription_tokens, get_user_tokens
from .tenants import Tenant, get_tenant, is_tenant_admin

settings = get_settings()
logger = logging.getLogger(__name__)

notifications_router = APIRouter(prefix="/tenants/{tenant_id}/notifications", tags=["notifications"])


# --- Data class ---


@dataclass
class Notification:
    tenant_id: str
    channel_id: str
    title: str
    body: str
    sent_by: str
    id: str = field(default_factory=lambda: str(uuid1()))
    sent_at: str = field(default_factory=lambda: str(datetime.now(timezone.utc)))


# --- API Models ---


class NotificationCreate(BaseModel):
    channel_ids: list[str] = Field(default_factory=list)
    include_child_channels: bool = True
    title: str
    body: str


class NotificationRead(BaseModel):
    id: str
    tenant_id: str
    channel_id: str
    title: str
    body: str
    sent_by: str | None = None
    sent_at: str


class DirectNotificationCreate(BaseModel):
    user_id: str
    title: str
    body: str


# --- Init function ---


async def notifications_init() -> None:
    create_table = """
        CREATE TABLE IF NOT EXISTS notifications (
            id text PRIMARY KEY,
            data jsonb NOT NULL
        )
    """
    await db_execute(create_table)
    await db_execute("CREATE INDEX IF NOT EXISTS idx_tenant_id ON notifications ((lower(data->>'tenant_id')))")
    await db_execute("CREATE INDEX IF NOT EXISTS idx_channel_id ON notifications ((lower(data->>'channel_id')))")

    return


# --- Notification send function ---


async def send_notification(tenant_id: str, channel: str, msg: Notification, save: bool = True) -> None:
    msg_tokens = await get_subscription_tokens(tenant_id, channel)
    if msg_tokens:
        await firebase_send(msg_tokens, msg)
        if save:
            await db_execute("INSERT INTO notifications (id, data) VALUES ($1, $2)", msg.id, asdict(msg))
    return


# async def send_direct_notification(tenant_id: str, msg_tokens: list[str], msg: Notification, save: bool = True) -> None:
#     if msg_tokens:
#         await firebase_send(msg_tokens, msg)
#         if save:
#             await db_execute("INSERT INTO notifications (id, data) VALUES ($1, $2)", msg.id, asdict(msg))
#     return


# --- API functions ---


@notifications_router.get(
    "",
    response_model=list[NotificationRead],
    status_code=status.HTTP_200_OK,
    response_description="Notification list",
)
async def list_notifications(
    tenant: Tenant = Depends(get_tenant),
    channel_ids: list[str] | None = Query(default=None, alias="channel"),
    limit: int = Query(default=10, ge=1, le=50),
    user: AuthUser = Depends(require_auth_user),
):
    if not channel_ids:
        rows = await db_fetch(
            "SELECT data FROM subscriptions WHERE data->>'tenant_id' = $1 AND data->>'user_id' = $2",
            tenant.id,
            user.preferred_username,
        )
        channel_ids = [d["data"]["channel_id"] for d in rows] if rows else []
    channel_ids.append(user.preferred_username)  # Add direct notifications in history list

    query = "SELECT data FROM notifications WHERE data->>'tenant_id' = $1 AND data->>'channel_id' = ANY($2::text[]) ORDER BY data->>'sent_at' DESC LIMIT $3"
    rows = await db_fetch(query, tenant.id, channel_ids, limit)
    return [d["data"] for d in rows] if rows else []


@notifications_router.post("", response_model=NotificationRead, status_code=status.HTTP_202_ACCEPTED)
async def send_notifications(
    payload: NotificationCreate,
    tenant: Tenant = Depends(get_tenant),
    user: AuthUser = Depends(require_auth_user),
):
    if not payload.channel_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one channel id is required.",
        )
    if not is_tenant_admin(tenant, user.preferred_username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required.",
        )

    for channel_id in payload.channel_ids:
        msg = Notification(
            tenant_id=tenant.id, channel_id=channel_id, title=payload.title, body=payload.body, sent_by=str(user)
        )
        await send_notification(tenant.id, channel_id, msg)

    return asdict(msg)


@notifications_router.post("/direct", response_model=NotificationRead, status_code=status.HTTP_202_ACCEPTED)
async def send_direct_notifications(
    payload: DirectNotificationCreate,
    tenant: Tenant = Depends(get_tenant),
    user: AuthUser = Depends(require_auth_user),
):
    if not payload.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user id is required.",
        )
    if not is_tenant_admin(tenant, user.preferred_username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required.",
        )

    user_tokens = await get_user_tokens(tenant.id, payload.user_id)
    if not user_tokens:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="User notification registration required.",
        )
    msg = Notification(
        tenant_id=tenant.id, channel_id=payload.user_id, title=payload.title, body=payload.body, sent_by=str(user)
    )
    await firebase_send(user_tokens, msg)
    await db_execute("INSERT INTO notifications (id, data) VALUES ($1, $2)", msg.id, asdict(msg))
    return asdict(msg)
