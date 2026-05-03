import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from .authentication import AuthUser, optional_auth_user, require_auth_user
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
    message: str  # JSON string: {"notification": {...}, ...}
    channel: str
    sent_at: str


# --- API models ---


class TokenCreate(BaseModel):
    tokens: list[str]


class NotificationTranslation(BaseModel):
    title: str
    body: str


class NotificationCreate(BaseModel):
    channels: list[str]
    notification: dict[str, NotificationTranslation]  # keyed by language code, e.g. "en", "sv"
    category: str | None = None
    important: bool = False
    link: str | None = None


class NotificationRead(BaseModel):
    id: int
    channel_id: str
    title: str  # compat: English title extracted from message
    body: str  # compat: English body extracted from message
    message: str
    sent_at: str


def is_sender(user: AuthUser) -> bool:
    return "notification-sender" in user.roles


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


@notifications_router.get("/notifications", response_model=list[NotificationRead], status_code=status.HTTP_200_OK)
async def list_notifications(
    user: AuthUser | None = Depends(optional_auth_user),
    count: int = Query(default=50, ge=1, le=200),
    not_before: str | None = Query(default=None),
    not_after: str | None = Query(default=None),
    channel: list[str] | None = Query(default=None),  # ignored, kept for compat
):
    user_channels = ["@all"]
    if user is not None:
        row = await db_fetchrow(
            "SELECT channels FROM users WHERE user_id = $1",
            user.preferred_username,
        )
        if row:
            user_channels.extend(row["channels"])

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
        " WHERE " + " AND ".join(conditions) + f" ORDER BY timestamp DESC LIMIT ${len(params)}"
    )

    rows = await db_fetch(query, *params)
    result = []
    for r in rows:
        msg = json.loads(r["message"])
        en = msg.get("notification", {}).get("en", {})
        result.append(
            NotificationRead(
                id=r["id"],
                channel_id=r["channel"],
                message=r["message"],
                title=en.get("title", ""),
                body=en.get("body", ""),
                sent_at=r["timestamp"].isoformat(),
            )
        )
    return result


@notifications_router.post("/notifications", status_code=status.HTTP_200_OK)
async def send_notification(
    payload: NotificationCreate,
    user: AuthUser = Depends(require_auth_user),
):
    if not is_sender(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")
    if "en" not in payload.notification:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="English translation ('en') is required."
        )

    now = datetime.now(timezone.utc)
    message_json = json.dumps(
        {
            "notification": {lang: t.model_dump() for lang, t in payload.notification.items()},
            "category": payload.category,
            "important": payload.important,
            "link": payload.link,
        }
    )

    # Collect unique tokens across all target channels, then send once
    all_tokens: set[str] = set()
    if "@all" in payload.channels:
        await db_execute(
            "INSERT INTO messages (channel, message, timestamp) VALUES ($1, $2, $3)",
            "@all", message_json, now,
        )
        rows = await db_fetch("SELECT tokens FROM users")
        for r in rows:
            all_tokens.update(r["tokens"])
    else:
        for channel in payload.channels:
            await db_execute(
                "INSERT INTO messages (channel, message, timestamp) VALUES ($1, $2, $3)",
                channel, message_json, now,
            )
            rows = await db_fetch("SELECT tokens FROM users WHERE $1 = ANY(channels)", channel)
            if rows:
                for r in rows:
                    all_tokens.update(r["tokens"])

    if all_tokens:
        en = payload.notification["en"]
        await firebase_send(list(all_tokens), en.title, en.body, message_json)

    return {"status": "ok"}
