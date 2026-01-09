import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .authenctication import AuthUser, require_auth_user
from .channels import get_channel_id
from .config import get_settings
from .db import db_execute, db_fetch, db_fetchrow
from .tenants import get_tenant_id, is_tenant_admin

settings = get_settings()
logger = logging.getLogger(__name__)

subscriptions_router = APIRouter(prefix="/tenants/{tenant_id}", tags=["subscriptions"])


# --- Data classes ---


@dataclass
class Tokens:
    id: str
    tenant_id: str
    device_tokens: list[str]
    updated_at: str = field(default_factory=lambda: str(datetime.now(timezone.utc)))


@dataclass
class Subscription:
    id: str
    tenant_id: str
    channel_id: str
    user_id: str
    created_at: str = field(default_factory=lambda: str(datetime.now(timezone.utc)))


# --- API Models ---


class TokenCreate(BaseModel):
    device_tokens: list[str]


class SubscriptionCreate(BaseModel):
    # user_id: str | None = None
    # device_tokens: list[str]
    pass


class SubscriptionRead(BaseModel):
    id: str
    tenant_id: str
    channel_id: str
    user_id: str
    # device_tokens: list[str]


# --- Init function ---


async def subscriptions_init() -> None:
    create_table = """
        CREATE TABLE IF NOT EXISTS tokens (
            id text PRIMARY KEY,
            data jsonb NOT NULL
        )
    """
    await db_execute(create_table)

    create_table = """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id text PRIMARY KEY,
            data jsonb NOT NULL
        )
    """
    await db_execute(create_table)
    await db_execute("CREATE INDEX IF NOT EXISTS idx_tenant_id ON subscriptions ((lower(data->>'tenant_id')))")
    await db_execute("CREATE INDEX IF NOT EXISTS idx_channel_id ON subscriptions ((lower(data->>'channel_id')))")
    await db_execute("CREATE INDEX IF NOT EXISTS idx_user_id ON subscriptions ((lower(data->>'user_id')))")

    return


# --- Subscription and token functions ---


async def get_subscription_tokens(tenant: str, channel: str) -> list[str]:
    rows = await db_fetch(
        "SELECT data FROM subscriptions WHERE data->>'tenant_id' = $1 AND data->>'channel_id' = $2", tenant, channel
    )
    if not rows:
        return []
    user_id_list = [f"{d['data']['user_id']}:{tenant}" for d in rows]
    rows = await db_fetch("SELECT data FROM tokens WHERE id = ANY($1::text[])", user_id_list)
    return [t for d in rows for t in d["data"]["device_tokens"]] if rows else []


async def get_user_tokens(tenant: str, user_id: str) -> list[str]:
    token_id = f"{user_id}:{tenant}"
    row = await db_fetchrow("SELECT data FROM tokens WHERE id = $1", token_id)
    return [t for t in row["data"]["device_tokens"]] if row else []


# --- API functions ---


@subscriptions_router.post(
    "/tokens",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def save_user_token(
    payload: TokenCreate,
    tenant: str = Depends(get_tenant_id),
    user: AuthUser = Depends(require_auth_user),
):
    """
    Save notifications tokens
    """
    uid = user.preferred_username
    token_id = f"{uid}:{tenant}"
    data = Tokens(
        id=token_id,
        tenant_id=tenant,
        device_tokens=payload.device_tokens,
    )
    row = await db_fetchrow("SELECT data FROM tokens WHERE id=$1", token_id)
    if row:
        existing_tokens = set(row["data"]["device_tokens"])
        new_tokens = set(payload.device_tokens)
        if new_tokens.issubset(existing_tokens):
            return  # No new tokens!
        data.device_tokens = list(existing_tokens | new_tokens)

    await db_execute(
        "INSERT INTO tokens (id, data) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
        token_id,
        asdict(data),
    )
    return


@subscriptions_router.get(
    "/subscriptions/me",
    response_model=list[SubscriptionRead],
    status_code=status.HTTP_200_OK,
    response_description="Subscription list",
)
async def list_subscriptions(
    tenant: str = Depends(get_tenant_id),
    user: AuthUser = Depends(require_auth_user),
):
    """
    Returns a list of subscribed notification channels for the current user
    """
    uid = user.preferred_username
    rows = await db_fetch(
        "SELECT data FROM subscriptions WHERE data->>'tenant_id' = $1 AND data->>'user_id' = $2", tenant, uid
    )
    return [d["data"] for d in rows] if rows else []


@subscriptions_router.post(
    "/channels/{channel_id}/subscriptions",
    response_model=SubscriptionRead,
    status_code=status.HTTP_201_CREATED,
)
async def subscribe_to_channel(
    # payload: SubscriptionCreate,
    tenant: str = Depends(get_tenant_id),
    channel: str = Depends(get_channel_id),
    user: AuthUser = Depends(require_auth_user),
):
    """
    Subscribe to a notification channel
    """
    # if payload.user_id and not await is_tenant_admin(tenant, user.preferred_username):
    #     raise HTTPException(
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         detail="Only tenant admins can register other users.",
    #     )
    uid = user.preferred_username
    data = Subscription(id=f"{uid}@{channel}:{tenant}", tenant_id=tenant, channel_id=channel, user_id=uid)
    await db_execute(
        "INSERT INTO subscriptions (id, data) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
        data.id,
        asdict(data),
    )
    return data


@subscriptions_router.delete(
    "/channels/{channel_id}/subscriptions",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unsubscribe_from_channel(
    tenant: str = Depends(get_tenant_id),
    channel: str = Depends(get_channel_id),
    user: AuthUser = Depends(require_auth_user),
):
    """
    Leave a notification channel
    """
    uid = user.preferred_username
    row = await db_fetchrow(
        "SELECT data FROM subscriptions WHERE data->>'tenant_id' = $1 AND data->>'channel_id' = $2 AND data->>'user_id' = $3",
        tenant,
        channel,
        uid,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found.",
        )
    await db_execute("DELETE FROM subscriptions WHERE id=$1", row["data"]["id"])
    return
