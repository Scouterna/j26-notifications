import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel

from .authentication import AuthUser, optional_auth_user, require_auth_user
from .db import db_execute, db_fetch, db_fetchrow
from .firebase import firebase_send
from .keycloak import get_group_members, get_user_groups

logger = logging.getLogger(__name__)

TENANT_PREFIX = "/tenants/jamboree26"
notifications_router = APIRouter(prefix=TENANT_PREFIX, tags=["notifications"])


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
    body: str   # compat: English body extracted from message
    message: str
    sent_at: str


# --- Helpers ---


def is_sender(user: AuthUser) -> bool:
    return "j26-notifications:notification-sender" in user.roles


async def _resolve_user_channels(user: AuthUser) -> list[str]:
    """Return the user's channels from DB, auto-registering with empty tokens if not found."""
    row = await db_fetchrow("SELECT channels FROM users WHERE user_id = $1", user.preferred_username)
    if row:
        return list(row["channels"])
    channels = await get_user_groups(user.preferred_username)
    channels.append(user.preferred_username)
    await db_execute(
        "INSERT INTO users (user_id, channels, tokens) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        user.preferred_username, channels, [],
    )
    return channels


async def _fetch_messages(
    channels: list[str],
    count: int,
    not_before: str | None,
    not_after: str | None,
) -> list[NotificationRead]:
    params: list = [channels]
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
    return [_row_to_notification(r) for r in rows]


def _row_to_notification(r) -> NotificationRead:
    msg = json.loads(r["message"])
    en = msg.get("notification", {}).get("en", {})
    return NotificationRead(
        id=r["id"],
        channel_id=r["channel"],
        message=r["message"],
        title=en.get("title", ""),
        body=en.get("body", ""),
        sent_at=r["timestamp"].isoformat(),
    )


async def _sync_channel_members(channel: str) -> None:
    """Refresh group membership from Keycloak and upsert each member into users table.
    If Keycloak is unavailable, logs a warning and skips the sync."""
    try:
        usernames = await get_group_members(channel)
        for username in usernames:
            user_channels = await get_user_groups(username)
            user_channels.append(username)
            await db_execute(
                """
                INSERT INTO users (user_id, channels, tokens)
                VALUES ($1, $2, '{}')
                ON CONFLICT (user_id) DO UPDATE
                    SET channels = EXCLUDED.channels
                """,
                username, user_channels,
            )
    except Exception as exc:
        logger.warning("Keycloak sync failed for channel %s, using existing DB data: %s", channel, exc)


async def _persist_and_collect_tokens(channels: list[str], message_json: str, now: datetime) -> set[str]:
    """Sync group members from Keycloak, insert a message row per channel, and return all unique FCM tokens."""
    tokens: set[str] = set()
    for channel in channels:
        if channel.startswith("/"):
            await _sync_channel_members(channel)
        await db_execute(
            "INSERT INTO messages (channel, message, timestamp) VALUES ($1, $2, $3)",
            channel, message_json, now,
        )
        rows = await db_fetch("SELECT tokens FROM users WHERE $1 = ANY(channels)", channel)
        for r in rows:
            tokens.update(r["tokens"])
    return tokens


async def _persist_and_collect_all_tokens(message_json: str, now: datetime) -> set[str]:
    """Insert a single @all message row and return tokens from all registered users."""
    await db_execute(
        "INSERT INTO messages (channel, message, timestamp) VALUES ($1, $2, $3)",
        "@all", message_json, now,
    )
    rows = await db_fetch("SELECT tokens FROM users")
    return {token for r in rows for token in r["tokens"]}


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
                tokens   = ARRAY(
                    SELECT DISTINCT unnest(users.tokens || EXCLUDED.tokens)
                )
        """,
        user.preferred_username, channels, payload.tokens,
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
    channels = ["@all"]
    if user is not None:
        channels.extend(await _resolve_user_channels(user))
    return await _fetch_messages(channels, count, not_before, not_after)


async def _do_send_notification(payload: NotificationCreate) -> None:
    now = datetime.now(timezone.utc)
    message_json = json.dumps({
        "notification": {lang: t.model_dump() for lang, t in payload.notification.items()},
        "category": payload.category,
        "important": payload.important,
        "link": payload.link,
    })

    if "@all" in payload.channels:
        tokens = await _persist_and_collect_all_tokens(message_json, now)
    else:
        tokens = await _persist_and_collect_tokens(payload.channels, message_json, now)

    if tokens:
        en = payload.notification["en"]
        await firebase_send(list(tokens), en.title, en.body, message_json)


@notifications_router.post("/notifications", status_code=status.HTTP_202_ACCEPTED)
async def send_notification(
    payload: NotificationCreate,
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(require_auth_user),
):
    if not is_sender(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")
    if "en" not in payload.notification:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="English translation ('en') is required.")

    background_tasks.add_task(_do_send_notification, payload)
    return {"status": "accepted"}
