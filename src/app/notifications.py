import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel

from .authentication import AuthUser, optional_auth_user, require_auth_user
from .db import db_execute, db_fetch, db_fetchrow
from .firebase import firebase_send
from .keycloak import get_all_groups, get_group_members, get_user_groups

logger = logging.getLogger(__name__)
_send_lock = asyncio.Lock()

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
    channels: list[str]
    title: str  # compat: English title extracted from message
    body: str  # compat: English body extracted from message
    message: str
    sent_at: str
    sender: str
    important: bool


class NotificationSent(BaseModel):
    id: int
    status: str


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
        user.preferred_username,
        channels,
        [],
    )
    return channels


async def _fetch_notifications(
    channels: list[str],
    count: int,
    not_before: str | None,
    not_after: str | None,
    important_prio: bool,
) -> list[NotificationRead]:
    base_conditions = ["channels && $1"]
    base_params: list = [channels]

    paged_conditions = list(base_conditions)
    paged_params = list(base_params)
    if not_before:
        paged_params.append(not_before)
        paged_conditions.append(f"timestamp > ${len(paged_params)}::timestamptz")
    if not_after:
        paged_params.append(not_after)
        paged_conditions.append(f"timestamp < ${len(paged_params)}::timestamptz")
    paged_params.append(count)
    paged_query = (
        "SELECT id, channels, message, timestamp, sender, important FROM notifications"
        " WHERE " + " AND ".join(paged_conditions) + f" ORDER BY timestamp DESC LIMIT ${len(paged_params)}"
    )
    paged_rows = await db_fetch(paged_query, *paged_params)
    paged = [_row_to_notification(r) for r in paged_rows]

    if not important_prio:
        return paged

    imp_query = (
        "SELECT id, channels, message, timestamp, sender, important FROM notifications"
        " WHERE " + " AND ".join(base_conditions) + " AND important ORDER BY timestamp DESC"
    )
    imp_rows = await db_fetch(imp_query, *base_params)
    important_items = [_row_to_notification(r) for r in imp_rows]

    # Merge: important first, then paged — deduped by id
    seen: set[int] = {n.id for n in important_items}
    merged = important_items + [n for n in paged if n.id not in seen]
    return merged


def _row_to_notification(r) -> NotificationRead:
    msg = json.loads(r["message"])
    translations = msg.get("notification", {})
    t = translations.get("en") or translations.get("sv") or {}
    return NotificationRead(
        id=r["id"],
        channels=list(r["channels"]),
        message=r["message"],
        title=t.get("title", ""),
        body=t.get("body", ""),
        sent_at=r["timestamp"].isoformat(),
        sender=r["sender"],
        important=r["important"],
    )


async def _sync_channel_members(channel: str) -> None:
    """Refresh group membership from Keycloak, only writing to DB for users whose channels changed.
    If Keycloak is unavailable, logs a warning and skips the sync."""
    try:
        usernames = await get_group_members(channel)
        if not usernames:
            return

        # Fetch current DB state for all members in one query
        rows = await db_fetch(
            "SELECT user_id, channels FROM users WHERE user_id = ANY($1)",
            usernames,
        )
        db_channels: dict[str, set[str]] = {r["user_id"]: set(r["channels"]) for r in rows}

        t0 = time.perf_counter()
        updates = 0
        for username in usernames:
            kc_channels = await get_user_groups(username)
            kc_channels.append(username)
            kc_set = set(kc_channels)
            if db_channels.get(username) == kc_set:
                continue  # no change, skip DB write
            updates += 1
            await db_execute(
                """
                INSERT INTO users (user_id, channels, tokens)
                VALUES ($1, $2, '{}')
                ON CONFLICT (user_id) DO UPDATE
                    SET channels = EXCLUDED.channels
                """,
                username,
                kc_channels,
            )
        logger.debug(
            "Keycloak sync for %s: %d members, %d updated, %.0fms",
            channel,
            len(usernames),
            updates,
            (time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:
        logger.warning("Keycloak sync failed for channel %s, using existing DB data: %s", channel, exc)


async def _insert_notification(channels: list[str], message_json: str, now: datetime, sender: str, important: bool) -> int:
    row = await db_fetchrow(
        "INSERT INTO notifications (channels, message, timestamp, sender, important) VALUES ($1, $2, $3, $4, $5) RETURNING id",
        channels,
        message_json,
        now,
        sender,
        important,
    )
    return row["id"]


async def _collect_tokens(channels: list[str]) -> set[str]:
    t0 = time.perf_counter()
    tokens: set[str] = set()
    for channel in channels:
        if channel.startswith("/"):
            await _sync_channel_members(channel)
        rows = await db_fetch("SELECT tokens FROM users WHERE $1 = ANY(channels)", channel)
        for r in rows:
            tokens.update(r["tokens"])
    logger.info(
        "Token collection for %s: %.0fms, %d tokens",
        channels,
        (time.perf_counter() - t0) * 1000,
        len(tokens),
    )
    return tokens


async def _collect_all_tokens() -> set[str]:
    rows = await db_fetch("SELECT tokens FROM users")
    return {token for r in rows for token in r["tokens"]}


async def _do_send_notification(tokens: set[str], message_json: str) -> None:
    async with _send_lock:
        if tokens:
            await firebase_send(list(tokens), message_json)


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
    important_prio: bool = Query(default=True),
    channel: list[str] | None = Query(default=None),  # ignored, kept for compat
):
    channels = ["@all"]
    if user is not None:
        channels.extend(await _resolve_user_channels(user))
    return await _fetch_notifications(channels, count, not_before, not_after, important_prio)


@notifications_router.get("/groups", response_model=list[str], status_code=status.HTTP_200_OK)
async def list_groups():
    """Return all valid channel groups from Keycloak (paths stripped of the
    /j26-scoutid-sync prefix). Public endpoint."""
    return await get_all_groups()


@notifications_router.post("/notifications", response_model=NotificationSent, status_code=status.HTTP_202_ACCEPTED)
async def send_notification(
    payload: NotificationCreate,
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(require_auth_user),
):
    if not is_sender(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sender privileges required.")
    if "en" not in payload.notification and "sv" not in payload.notification:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one of 'en' or 'sv' translation is required.",
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

    msg_id = await _insert_notification(payload.channels, message_json, now, user.preferred_username, payload.important)

    if "@all" in payload.channels:
        tokens = await _collect_all_tokens()
    else:
        tokens = await _collect_tokens(payload.channels)

    background_tasks.add_task(_do_send_notification, tokens, message_json)

    return {"id": msg_id, "status": "accepted"}
