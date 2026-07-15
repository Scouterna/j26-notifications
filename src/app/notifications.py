import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from .authentication import AuthUser, optional_auth_user, require_auth_user
from .db import db_execute, db_fetch, db_fetchrow
from .firebase import firebase_send
from .groups import group_path_id_to_name, group_path_name_to_id
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


class ImportantNotificationRead(BaseModel):
    id: int
    notification: dict[str, NotificationTranslation]  # keyed by language code, e.g. "en", "sv"
    important: bool
    sent_at: str
    channels: list[str]  # channel names (group paths translated back from IDs), for row context


class NotificationPatch(BaseModel):
    important: bool


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


def _row_to_important(r) -> ImportantNotificationRead:
    msg = json.loads(r["message"])
    translations = msg.get("notification", {})
    return ImportantNotificationRead(
        id=r["id"],
        notification={lang: NotificationTranslation(**t) for lang, t in translations.items()},
        important=r["important"],
        sent_at=r["timestamp"].isoformat(),
        channels=[group_path_id_to_name(c) for c in r["channels"]],
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


async def _insert_notification(
    channels: list[str], message_json: str, now: datetime, sender: str, important: bool
) -> int:
    row = await db_fetchrow(
        "INSERT INTO notifications (channels, message, timestamp, sender, important) VALUES ($1, $2, $3, $4, $5) RETURNING id",
        channels,
        message_json,
        now,
        sender,
        important,
    )
    return row["id"]


async def _collect_tokens(channels: list[str]) -> dict[str, set[str]]:
    t0 = time.perf_counter()
    tokens_by_lang: dict[str, set[str]] = {}
    for channel in channels:
        if channel.startswith("/"):
            await _sync_channel_members(channel)
        rows = await db_fetch("SELECT tokens, language FROM users WHERE $1 = ANY(channels)", channel)
        for r in rows:
            tokens_by_lang.setdefault(r["language"], set()).update(r["tokens"])
    total = sum(len(t) for t in tokens_by_lang.values())
    logger.info(
        "Token collection for %s: %.0fms, %d tokens",
        channels,
        (time.perf_counter() - t0) * 1000,
        total,
    )
    return tokens_by_lang


async def _collect_all_tokens() -> dict[str, set[str]]:
    rows = await db_fetch("SELECT tokens, language FROM users")
    tokens_by_lang: dict[str, set[str]] = {}
    for r in rows:
        tokens_by_lang.setdefault(r["language"], set()).update(r["tokens"])
    return tokens_by_lang


def _pick_translation(notification: dict[str, NotificationTranslation], lang: str) -> NotificationTranslation:
    return notification.get(lang) or notification.get("en") or notification["sv"]


async def _do_send_notification(
    tokens_by_lang: dict[str, set[str]],
    notification: dict[str, NotificationTranslation],
    data_json: str,
    link: str | None,
    base_url: str,
) -> None:
    async with _send_lock:
        for lang, tokens in tokens_by_lang.items():
            if tokens:
                t = _pick_translation(notification, lang)
                await firebase_send(list(tokens), t.title, t.body, data_json, link, base_url)


# --- Endpoints ---


@notifications_router.post("/register", status_code=status.HTTP_200_OK)
@notifications_router.post("/tokens", status_code=status.HTTP_200_OK, include_in_schema=False)
async def register_users(
    payload: TokenCreate,
    user: AuthUser = Depends(require_auth_user),
    j26_language: str | None = Cookie(default=None, alias="j26-language"),
):
    language = j26_language if j26_language and len(j26_language) == 2 else "sv"
    channels = await get_user_groups(user.preferred_username)
    channels.append(user.preferred_username)
    await db_execute(
        """
        INSERT INTO users (user_id, channels, tokens, language)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id) DO UPDATE
            SET channels = EXCLUDED.channels,
                tokens   = ARRAY(
                    SELECT DISTINCT unnest(users.tokens || EXCLUDED.tokens)
                ),
                language = EXCLUDED.language
        """,
        user.preferred_username,
        channels,
        payload.tokens,
        language,
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
    request: Request,
    user: AuthUser = Depends(require_auth_user),
):
    if not is_sender(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sender privileges required.")
    if "en" not in payload.notification and "sv" not in payload.notification:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one of 'en' or 'sv' translation is required.",
        )

    # Translate "/group/<name>" -> "/group/<id>" so storage and member
    # resolution use numeric IDs; other channels pass through unchanged.
    channels = [group_path_name_to_id(c) for c in payload.channels]

    now = datetime.now(timezone.utc)
    message = {
        "notification": {lang: t.model_dump() for lang, t in payload.notification.items()},
        "category": payload.category,
        "important": payload.important,
        "link": payload.link,
    }
    msg_id = await _insert_notification(channels, json.dumps(message), now, user.preferred_username, payload.important)
    message["id"] = msg_id  # Help the client target the specific notification precisely
    del message["notification"]  # Don't pass the complete notification in the payload anymore

    if "@all" in channels:
        tokens_by_lang = await _collect_all_tokens()
    else:
        tokens_by_lang = await _collect_tokens(channels)

    background_tasks.add_task(
        _do_send_notification,
        tokens_by_lang,
        payload.notification,
        json.dumps(message),
        payload.link,
        str(request.base_url),
    )

    return {"id": msg_id, "status": "accepted"}


@notifications_router.get(
    "/notifications/important",
    response_model=list[ImportantNotificationRead],
    status_code=status.HTTP_200_OK,
)
async def list_important_notifications(user: AuthUser = Depends(require_auth_user)):
    """List notifications still flagged important, newest first. Sender-only."""
    if not is_sender(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sender privileges required.")
    rows = await db_fetch(
        "SELECT id, channels, message, timestamp, sender, important FROM notifications"
        " WHERE important ORDER BY timestamp DESC"
    )
    return [_row_to_important(r) for r in rows]


@notifications_router.patch(
    "/notifications/{notification_id}",
    status_code=status.HTTP_200_OK,
)
async def patch_notification(
    notification_id: int,
    payload: NotificationPatch,
    user: AuthUser = Depends(require_auth_user),
):
    """Set the `important` flag on a stored notification. State change only —
    does NOT re-send or re-dispatch. Sender-only."""
    if not is_sender(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sender privileges required.")

    row = await db_fetchrow(
        "UPDATE notifications SET important = $1,"
        " message = jsonb_set(message::jsonb, '{important}', to_jsonb($1::boolean))::text"
        " WHERE id = $2 RETURNING id",
        payload.important,
        notification_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.")
    return {"id": notification_id, "important": payload.important}
