import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from .authentication import AuthUser, require_auth_user
from .db import db_execute, db_fetch, db_fetchrow
from .firebase import firebase_send
from .keycloak import get_user_groups

logger = logging.getLogger(__name__)

TENANT_PREFIX = "/tenants/jamboree26"
notifications_router = APIRouter(prefix=TENANT_PREFIX, tags=["notifications"])


# --- Data classes ---


@dataclass
class User:
    user_id: str
    channels: list[str]
    tokens: list[str]


@dataclass
class Message:
    message: str  # JSON string: {"title": ..., "body": ...}
    channel: str
    timestamp: str


# --- API models ---


class TokenCreate(BaseModel):
    tokens: list[str]


class NotificationCreate(BaseModel):
    channel: str
    title: str
    body: str


class MessageRead(BaseModel):
    id: int
    channel: str
    message: str
    timestamp: str




def is_admin(user: AuthUser) -> bool:
    # STUB: e.g. return "admin" in user.roles
    return True


# --- Endpoints ---


@notifications_router.post("/register", status_code=status.HTTP_200_OK)
@notifications_router.post("/tokens", status_code=status.HTTP_200_OK, include_in_schema=False)
async def register_users(
    payload: TokenCreate,
    user: AuthUser = Depends(require_auth_user),
):
    channels = await get_user_groups(user.preferred_username)
    channels.append(user.preferred_username)
    await db_execute(
        """
        INSERT INTO users (user_id, channels, tokens)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE
            SET channels = EXCLUDED.channels,
                tokens   = EXCLUDED.tokens
        """,
        user.preferred_username,
        channels,
        payload.tokens,
    )
    return {"status": "ok"}


@notifications_router.get("/notifications", response_model=list[MessageRead], status_code=status.HTTP_200_OK)
async def list_notifications(
    user: AuthUser = Depends(require_auth_user),
    count: int = Query(default=50, ge=1, le=200),
    not_before: str | None = Query(default=None),
    not_after: str | None = Query(default=None),
    channel: list[str] | None = Query(default=None),  # ignored, kept for compat
):
    row = await db_fetchrow(
        "SELECT channels FROM users WHERE user_id = $1",
        user.preferred_username,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not registered.")
    user_channels: list[str] = row["channels"]
    if not user_channels:
        return []

    params: list = [user_channels]
    conditions = ["channel = ANY($1)"]
    if not_before:
        params.append(not_before)
        conditions.append(f"timestamp > ${len(params)}::timestamptz")
    if not_after:
        params.append(not_after)
        conditions.append(f"timestamp < ${len(params)}::timestamptz")
    params.append(count)
    query = (
        "SELECT id, channel, message, timestamp FROM messages"
        " WHERE " + " AND ".join(conditions) +
        f" ORDER BY timestamp DESC LIMIT ${len(params)}"
    )

    rows = await db_fetch(query, *params)
    return (
        [
            MessageRead(
                id=r["id"],
                channel=r["channel"],
                message=r["message"],
                timestamp=r["timestamp"].isoformat(),
            )
            for r in rows
        ]
        if rows
        else []
    )


@notifications_router.post("/notifications", status_code=status.HTTP_200_OK)
async def send_notification(
    payload: NotificationCreate,
    user: AuthUser = Depends(require_auth_user),
):
    if not is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")

    message_json = json.dumps({"title": payload.title, "body": payload.body})
    now = datetime.now(timezone.utc)

    await db_execute(
        "INSERT INTO messages (channel, message, timestamp) VALUES ($1, $2, $3)",
        payload.channel,
        message_json,
        now,
    )

    rows = await db_fetch(
        "SELECT tokens FROM users WHERE $1 = ANY(channels)",
        payload.channel,
    )
    all_tokens = [token for r in rows for token in r["tokens"]] if rows else []

    if all_tokens:
        await firebase_send(all_tokens, payload.title, payload.body)

    return {"status": "ok"}
